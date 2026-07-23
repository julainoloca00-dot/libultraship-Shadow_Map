from pathlib import Path
import re


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def regex_replace_once(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one regex match, found {count}")
    return updated


# -------------------------------------------------------------------------------------------------
# Rendering API: optional backend hook. Non-DX11 backends keep the no-op default.
# -------------------------------------------------------------------------------------------------
path = Path("include/fast/backends/gfx_rendering_api.h")
text = path.read_text(encoding="utf-8")
text = replace_once(text, '#include <stdint.h>\n', '#include <stdint.h>\n#include <cstddef>\n', "gfx api cstddef")
text = replace_once(
    text,
    '    virtual void SetSrgbMode() = 0;\n    virtual ImTextureID GetTextureById(int id) = 0;\n',
    '''    virtual void SetSrgbMode() = 0;

    // SOH [Enhancement] Optimized dynamic shadow mapping. The interpreter supplies the previous
    // frame's camera-visible caster triangles in world space. Backends that do not implement this
    // hook retain the existing shadow path; DX11 renders a low-resolution depth map and resolves it
    // over the scene with PCF.
    virtual void RenderDynamicShadowMap(const float* worldVertices, size_t vertexCount,
                                        const float* cameraWorldToClip, const float lightDir[3],
                                        uint32_t resolution, float opacity, float bias, int pcfRadius) {
    }

    virtual ImTextureID GetTextureById(int id) = 0;
''',
    "gfx api shadow hook",
)
path.write_text(text, encoding="utf-8")


# -------------------------------------------------------------------------------------------------
# Interpreter state: keep raw world-space casters instead of generating CPU stencil volumes.
# -------------------------------------------------------------------------------------------------
path = Path("include/fast/interpreter.h")
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''    std::vector<float> mShadowVerts;
    // SOH [Enhancement] Actor shadow: opacity bands the soft edge is built from. Band 0 is the full-opacity
''',
    '''    std::vector<float> mShadowVerts;
    // SOH [Enhancement] Dynamic shadow map: raw world-space caster triangles captured during the
    // current actor pass and consumed at the next frame's environment/actor boundary. This preserves
    // the existing one-frame deferred behavior while eliminating CPU footprint rasterization and
    // stencil-volume generation.
    std::vector<float> mShadowCasterAccum;
    float mShadowLightDirAccum[3] = { 0.0f, 0.0f, 0.0f };
    uint32_t mShadowLightDirSamples = 0;
    static constexpr uint32_t kDynamicShadowMapResolution = 512;
    static constexpr float kDynamicShadowMapBias = 0.0015f;
    static constexpr int kDynamicShadowMapPcfRadius = 1; // 3x3 Percentage-Closer Filtering

    // SOH [Enhancement] Actor shadow: opacity bands the soft edge is built from. Band 0 is the full-opacity
''',
    "interpreter shadow map state",
)
path.write_text(text, encoding="utf-8")


# -------------------------------------------------------------------------------------------------
# Interpreter implementation: capture visible casters and ask the backend to create/resolve the map.
# -------------------------------------------------------------------------------------------------
path = Path("src/fast/interpreter.cpp")
text = path.read_text(encoding="utf-8")
new_flush = r'''void Interpreter::FlushToonShadow() {
    const float coreAlpha = std::clamp(mToonShadowAlpha, 0.0f, 1.0f);
    if (mShadowVerts.size() < 9 || coreAlpha <= 0.0f || !mRdp->toon_shadow) {
        mShadowVerts.clear();
        return;
    }

    // Keep the buffer bounded. The data is three floats per vertex and is consumed once per frame.
    // Drop newest casters once the budget is full, matching the old volume accumulator's behavior.
    const size_t available =
        mShadowCasterAccum.size() < kShadowAccumBudgetFloats
            ? kShadowAccumBudgetFloats - mShadowCasterAccum.size()
            : 0;
    const size_t copyFloats = std::min(mShadowVerts.size(), available - (available % 9));
    if (copyFloats >= 9) {
        mShadowCasterAccum.insert(mShadowCasterAccum.end(), mShadowVerts.begin(),
                                  mShadowVerts.begin() + copyFloats);

        // A single optimized directional map uses the average dominant key of all accepted casters.
        // Per-object point-light maps would require one map and resolve per actor and defeat the low-cost goal.
        const float dx = mRsp->toon_shadow_dir[0];
        const float dy = mRsp->toon_shadow_dir[1];
        const float dz = mRsp->toon_shadow_dir[2];
        const float len2 = dx * dx + dy * dy + dz * dz;
        if (len2 > 1e-8f) {
            const float invLen = 1.0f / sqrtf(len2);
            mShadowLightDirAccum[0] += dx * invLen;
            mShadowLightDirAccum[1] += dy * invLen;
            mShadowLightDirAccum[2] += dz * invLen;
            mShadowLightDirSamples++;
        }
    }

    mShadowVerts.clear();
}'''
text = regex_replace_once(
    text,
    r'void Interpreter::FlushToonShadow\(\) \{.*?\n\}\n\n// SOH \[Enhancement\] Actor shadow: render every shadow volume',
    new_flush + '\n\n// SOH [Enhancement] Actor shadow: render every shadow volume',
    "replace FlushToonShadow",
)

new_render = r'''void Interpreter::RenderShadowVolumes() {
    float lightDir[3] = { 0.30f, 1.0f, 0.20f };
    if (mShadowLightDirSamples > 0) {
        lightDir[0] = mShadowLightDirAccum[0] / (float)mShadowLightDirSamples;
        lightDir[1] = mShadowLightDirAccum[1] / (float)mShadowLightDirSamples;
        lightDir[2] = mShadowLightDirAccum[2] / (float)mShadowLightDirSamples;
    }
    const float len2 = lightDir[0] * lightDir[0] + lightDir[1] * lightDir[1] + lightDir[2] * lightDir[2];
    if (len2 > 1e-8f) {
        const float invLen = 1.0f / sqrtf(len2);
        lightDir[0] *= invLen;
        lightDir[1] *= invLen;
        lightDir[2] *= invLen;
    }

    const size_t vertexCount = mShadowCasterAccum.size() / 3;
    mRapi->RenderDynamicShadowMap(vertexCount >= 3 ? mShadowCasterAccum.data() : nullptr, vertexCount,
                                  &mRsp->P_matrix[0][0], lightDir, kDynamicShadowMapResolution,
                                  std::clamp(mToonShadowAlpha, 0.0f, 1.0f), kDynamicShadowMapBias,
                                  kDynamicShadowMapPcfRadius);

    mShadowCasterAccum.clear();
    mShadowLightDirAccum[0] = mShadowLightDirAccum[1] = mShadowLightDirAccum[2] = 0.0f;
    mShadowLightDirSamples = 0;

    // Release any capacity retained by the legacy stencil-volume path after switching methods.
    for (int band = 0; band < kShadowBands; band++) {
        mShadowVolumeAccum[band].clear();
        mShadowVolumeKind[band].clear();
    }
}'''
text = regex_replace_once(
    text,
    r'void Interpreter::RenderShadowVolumes\(\) \{.*?\n\}\n\nvoid Interpreter::GfxDpSetGrayscaleColor',
    new_render + '\n\nvoid Interpreter::GfxDpSetGrayscaleColor',
    "replace RenderShadowVolumes",
)
path.write_text(text, encoding="utf-8")


# -------------------------------------------------------------------------------------------------
# DX11 declarations and resources.
# -------------------------------------------------------------------------------------------------
path = Path("include/fast/backends/gfx_direct3d_common.h")
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '''struct PerDrawCB {
''',
    '''struct DynamicShadowCB {
    float lightViewProj[16];
    float inverseCamera[16];
    // x = opacity, y = receiver bias, z = inverse map resolution, w = PCF radius
    float params[4];
};

struct PerDrawCB {
''',
    "dynamic shadow cb",
)
text = replace_once(
    text,
    '''    void SetSrgbMode() override;
    ImTextureID GetTextureById(int id) override;
''',
    '''    void SetSrgbMode() override;
    void RenderDynamicShadowMap(const float* worldVertices, size_t vertexCount,
                                const float* cameraWorldToClip, const float lightDir[3],
                                uint32_t resolution, float opacity, float bias, int pcfRadius) override;
    ImTextureID GetTextureById(int id) override;
''',
    "dx11 public shadow method",
)
text = replace_once(
    text,
    '''    void CreateDepthStencilObjects(uint32_t width, uint32_t height, uint32_t msaa_count, ID3D11DepthStencilView** view,
                                    ID3D11ShaderResourceView** srv);
''',
    '''    void CreateDepthStencilObjects(uint32_t width, uint32_t height, uint32_t msaa_count, ID3D11DepthStencilView** view,
                                    ID3D11ShaderResourceView** srv);
    void EnsureDynamicShadowResources(uint32_t resolution, size_t vertexBytes);
''',
    "dx11 private shadow helper",
)
text = replace_once(
    text,
    '''    Microsoft::WRL::ComPtr<ID3D11Buffer> mPerToonCb; // SOH [Enhancement] toon lighting (register b2)
    Microsoft::WRL::ComPtr<ID3D11Buffer> mCoordBuffer;
''',
    '''    Microsoft::WRL::ComPtr<ID3D11Buffer> mPerToonCb; // SOH [Enhancement] toon lighting (register b2)

    // SOH [Enhancement] 512x512 dynamic shadow map + deferred PCF resolve resources.
    Microsoft::WRL::ComPtr<ID3D11Texture2D> mDynamicShadowTexture;
    Microsoft::WRL::ComPtr<ID3D11DepthStencilView> mDynamicShadowDsv;
    Microsoft::WRL::ComPtr<ID3D11ShaderResourceView> mDynamicShadowSrv;
    Microsoft::WRL::ComPtr<ID3D11Buffer> mDynamicShadowVertexBuffer;
    Microsoft::WRL::ComPtr<ID3D11Buffer> mDynamicShadowCb;
    Microsoft::WRL::ComPtr<ID3D11VertexShader> mDynamicShadowDepthVs;
    Microsoft::WRL::ComPtr<ID3D11VertexShader> mDynamicShadowResolveVs;
    Microsoft::WRL::ComPtr<ID3D11PixelShader> mDynamicShadowResolvePs;
    Microsoft::WRL::ComPtr<ID3D11PixelShader> mDynamicShadowResolvePsMsaa;
    Microsoft::WRL::ComPtr<ID3D11InputLayout> mDynamicShadowInputLayout;
    Microsoft::WRL::ComPtr<ID3D11DepthStencilState> mDynamicShadowDepthState;
    Microsoft::WRL::ComPtr<ID3D11DepthStencilState> mDynamicShadowResolveDepthState;
    Microsoft::WRL::ComPtr<ID3D11RasterizerState> mDynamicShadowRasterState;
    Microsoft::WRL::ComPtr<ID3D11RasterizerState> mDynamicShadowResolveRasterState;
    Microsoft::WRL::ComPtr<ID3D11BlendState> mDynamicShadowBlendState;
    Microsoft::WRL::ComPtr<ID3D11SamplerState> mDynamicShadowComparisonSampler;
    DynamicShadowCB mDynamicShadowCbData{};
    uint32_t mDynamicShadowResolution = 0;
    size_t mDynamicShadowVertexBufferBytes = 0;

    Microsoft::WRL::ComPtr<ID3D11Buffer> mCoordBuffer;
''',
    "dx11 shadow members",
)
path.write_text(text, encoding="utf-8")


# -------------------------------------------------------------------------------------------------
# DX11 implementation. It renders caster depth, reconstructs world position from the main depth buffer,
# performs a 3x3 PCF comparison, and alpha-composites black only onto shadowed environment pixels.
# -------------------------------------------------------------------------------------------------
path = Path("src/fast/backends/gfx_direct3d11.cpp")
text = path.read_text(encoding="utf-8")

implementation = r'''
namespace {

static bool InvertShadowMatrix4x4(const float* src, float* dst) {
    float a[4][8] = {};
    for (int r = 0; r < 4; r++) {
        for (int c = 0; c < 4; c++) {
            a[r][c] = src[r * 4 + c];
        }
        a[r][r + 4] = 1.0f;
    }
    for (int col = 0; col < 4; col++) {
        int pivot = col;
        for (int r = col + 1; r < 4; r++) {
            if (fabsf(a[r][col]) > fabsf(a[pivot][col])) {
                pivot = r;
            }
        }
        if (fabsf(a[pivot][col]) < 1e-8f) {
            return false;
        }
        if (pivot != col) {
            for (int c = 0; c < 8; c++) {
                std::swap(a[pivot][c], a[col][c]);
            }
        }
        const float invPivot = 1.0f / a[col][col];
        for (int c = 0; c < 8; c++) {
            a[col][c] *= invPivot;
        }
        for (int r = 0; r < 4; r++) {
            if (r == col) {
                continue;
            }
            const float factor = a[r][col];
            for (int c = 0; c < 8; c++) {
                a[r][c] -= factor * a[col][c];
            }
        }
    }
    for (int r = 0; r < 4; r++) {
        for (int c = 0; c < 4; c++) {
            dst[r * 4 + c] = a[r][c + 4];
        }
    }
    return true;
}

static void NormalizeShadowVec(float v[3]) {
    const float len2 = v[0] * v[0] + v[1] * v[1] + v[2] * v[2];
    if (len2 < 1e-8f) {
        v[0] = 0.30f;
        v[1] = 1.0f;
        v[2] = 0.20f;
    }
    const float invLen = 1.0f / sqrtf(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
    v[0] *= invLen;
    v[1] *= invLen;
    v[2] *= invLen;
}

static void CrossShadowVec(const float a[3], const float b[3], float out[3]) {
    out[0] = a[1] * b[2] - a[2] * b[1];
    out[1] = a[2] * b[0] - a[0] * b[2];
    out[2] = a[0] * b[1] - a[1] * b[0];
}

static float DotShadowVec(const float a[3], const float b[3]) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

} // namespace

void GfxRenderingAPIDX11::EnsureDynamicShadowResources(uint32_t resolution, size_t vertexBytes) {
    const UINT compileFlags = D3DCOMPILE_OPTIMIZATION_LEVEL2;
    auto compile = [&](const char* source, const char* entry, const char* target, ID3DBlob** blob) {
        ComPtr<ID3DBlob> errors;
        HRESULT hr = mD3dCompile(source, strlen(source), nullptr, nullptr, nullptr, entry, target, compileFlags, 0, blob,
                                 errors.GetAddressOf());
        if (FAILED(hr)) {
            const char* errorText = errors ? (const char*)errors->GetBufferPointer() : "Unknown shadow shader error";
            MessageBoxA(mWindowBackend->GetWindowHandle(), errorText, "Dynamic shadow shader error",
                        MB_OK | MB_ICONERROR);
            throw hr;
        }
    };

    if (!mDynamicShadowDepthVs) {
        static const char* depthShader = R"SHADOW(
cbuffer DynamicShadowCB : register(b0) {
    row_major float4x4 lightViewProj;
    row_major float4x4 inverseCamera;
    float4 shadowParams;
};
struct VSInput { float3 position : POSITION; };
float4 VSMain(VSInput input) : SV_POSITION {
    return mul(float4(input.position, 1.0), lightViewProj);
}
)SHADOW";

        static const char* resolveShader = R"SHADOW(
Texture2D<float> sceneDepth : register(t0);
Texture2D<float> shadowDepth : register(t1);
SamplerComparisonState shadowComparison : register(s1);
cbuffer DynamicShadowCB : register(b0) {
    row_major float4x4 lightViewProj;
    row_major float4x4 inverseCamera;
    float4 shadowParams;
};
struct VSOutput { float4 position : SV_POSITION; float2 uv : TEXCOORD0; };
VSOutput VSMain(uint vertexId : SV_VertexID) {
    VSOutput output;
    float2 uv = float2((vertexId << 1) & 2, vertexId & 2);
    output.uv = uv;
    output.position = float4(uv * float2(2.0, -2.0) + float2(-1.0, 1.0), 0.0, 1.0);
    return output;
}
float ReadSceneDepth(int2 pixel) { return sceneDepth.Load(int3(pixel, 0)); }
float4 PSMain(VSOutput input) : SV_TARGET {
    uint width, height;
    sceneDepth.GetDimensions(width, height);
    int2 pixel = int2(min(input.uv * float2(width, height), float2(width - 1, height - 1)));
    float depth = ReadSceneDepth(pixel);
    if (depth >= 0.999999) discard;

    float4 world = mul(float4(input.uv.x * 2.0 - 1.0, 1.0 - input.uv.y * 2.0,
                              depth * 2.0 - 1.0, 1.0), inverseCamera);
    if (abs(world.w) < 1e-6) discard;
    world /= world.w;

    float4 lightClip = mul(world, lightViewProj);
    if (lightClip.w <= 0.0) discard;
    float3 lightNdc = lightClip.xyz / lightClip.w;
    float2 shadowUv = float2(lightNdc.x * 0.5 + 0.5, 0.5 - lightNdc.y * 0.5);
    if (shadowUv.x <= 0.0 || shadowUv.x >= 1.0 || shadowUv.y <= 0.0 || shadowUv.y >= 1.0 ||
        lightNdc.z <= 0.0 || lightNdc.z >= 1.0) discard;

    int radius = clamp((int)shadowParams.w, 0, 2);
    float lit = 0.0;
    float samples = 0.0;
    [unroll] for (int y = -2; y <= 2; y++) {
        [unroll] for (int x = -2; x <= 2; x++) {
            if (abs(x) <= radius && abs(y) <= radius) {
                lit += shadowDepth.SampleCmpLevelZero(shadowComparison,
                    shadowUv + float2(x, y) * shadowParams.z, lightNdc.z - shadowParams.y);
                samples += 1.0;
            }
        }
    }
    float shadow = 1.0 - lit / max(samples, 1.0);
    return float4(0.0, 0.0, 0.0, saturate(shadowParams.x * shadow));
}
)SHADOW";

        static const char* resolveShaderMsaa = R"SHADOW(
Texture2DMS<float> sceneDepth : register(t0);
Texture2D<float> shadowDepth : register(t1);
SamplerComparisonState shadowComparison : register(s1);
cbuffer DynamicShadowCB : register(b0) {
    row_major float4x4 lightViewProj;
    row_major float4x4 inverseCamera;
    float4 shadowParams;
};
struct VSOutput { float4 position : SV_POSITION; float2 uv : TEXCOORD0; };
VSOutput VSMain(uint vertexId : SV_VertexID) {
    VSOutput output;
    float2 uv = float2((vertexId << 1) & 2, vertexId & 2);
    output.uv = uv;
    output.position = float4(uv * float2(2.0, -2.0) + float2(-1.0, 1.0), 0.0, 1.0);
    return output;
}
float4 PSMain(VSOutput input) : SV_TARGET {
    uint width, height, sampleCount;
    sceneDepth.GetDimensions(width, height, sampleCount);
    int2 pixel = int2(min(input.uv * float2(width, height), float2(width - 1, height - 1)));
    float depth = sceneDepth.Load(pixel, 0);
    if (depth >= 0.999999) discard;
    float4 world = mul(float4(input.uv.x * 2.0 - 1.0, 1.0 - input.uv.y * 2.0,
                              depth * 2.0 - 1.0, 1.0), inverseCamera);
    if (abs(world.w) < 1e-6) discard;
    world /= world.w;
    float4 lightClip = mul(world, lightViewProj);
    if (lightClip.w <= 0.0) discard;
    float3 lightNdc = lightClip.xyz / lightClip.w;
    float2 shadowUv = float2(lightNdc.x * 0.5 + 0.5, 0.5 - lightNdc.y * 0.5);
    if (shadowUv.x <= 0.0 || shadowUv.x >= 1.0 || shadowUv.y <= 0.0 || shadowUv.y >= 1.0 ||
        lightNdc.z <= 0.0 || lightNdc.z >= 1.0) discard;
    int radius = clamp((int)shadowParams.w, 0, 2);
    float lit = 0.0;
    float samples = 0.0;
    [unroll] for (int y = -2; y <= 2; y++) {
        [unroll] for (int x = -2; x <= 2; x++) {
            if (abs(x) <= radius && abs(y) <= radius) {
                lit += shadowDepth.SampleCmpLevelZero(shadowComparison,
                    shadowUv + float2(x, y) * shadowParams.z, lightNdc.z - shadowParams.y);
                samples += 1.0;
            }
        }
    }
    float shadow = 1.0 - lit / max(samples, 1.0);
    return float4(0.0, 0.0, 0.0, saturate(shadowParams.x * shadow));
}
)SHADOW";

        ComPtr<ID3DBlob> depthVsBlob, resolveVsBlob, resolvePsBlob, resolvePsMsaaBlob;
        compile(depthShader, "VSMain", "vs_4_0", depthVsBlob.GetAddressOf());
        compile(resolveShader, "VSMain", "vs_4_0", resolveVsBlob.GetAddressOf());
        compile(resolveShader, "PSMain", "ps_4_0", resolvePsBlob.GetAddressOf());
        compile(resolveShaderMsaa, "PSMain", "ps_4_1", resolvePsMsaaBlob.GetAddressOf());
        ThrowIfFailed(mDevice->CreateVertexShader(depthVsBlob->GetBufferPointer(), depthVsBlob->GetBufferSize(), nullptr,
                                                  mDynamicShadowDepthVs.GetAddressOf()));
        ThrowIfFailed(mDevice->CreateVertexShader(resolveVsBlob->GetBufferPointer(), resolveVsBlob->GetBufferSize(), nullptr,
                                                  mDynamicShadowResolveVs.GetAddressOf()));
        ThrowIfFailed(mDevice->CreatePixelShader(resolvePsBlob->GetBufferPointer(), resolvePsBlob->GetBufferSize(), nullptr,
                                                  mDynamicShadowResolvePs.GetAddressOf()));
        ThrowIfFailed(mDevice->CreatePixelShader(resolvePsMsaaBlob->GetBufferPointer(), resolvePsMsaaBlob->GetBufferSize(), nullptr,
                                                  mDynamicShadowResolvePsMsaa.GetAddressOf()));
        D3D11_INPUT_ELEMENT_DESC input = { "POSITION", 0, DXGI_FORMAT_R32G32B32_FLOAT, 0, 0,
                                           D3D11_INPUT_PER_VERTEX_DATA, 0 };
        ThrowIfFailed(mDevice->CreateInputLayout(&input, 1, depthVsBlob->GetBufferPointer(), depthVsBlob->GetBufferSize(),
                                                 mDynamicShadowInputLayout.GetAddressOf()));

        D3D11_BUFFER_DESC cbDesc = {};
        cbDesc.ByteWidth = sizeof(DynamicShadowCB);
        cbDesc.Usage = D3D11_USAGE_DYNAMIC;
        cbDesc.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
        cbDesc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        ThrowIfFailed(mDevice->CreateBuffer(&cbDesc, nullptr, mDynamicShadowCb.GetAddressOf()));

        D3D11_DEPTH_STENCIL_DESC depthDesc = {};
        depthDesc.DepthEnable = TRUE;
        depthDesc.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ALL;
        depthDesc.DepthFunc = D3D11_COMPARISON_LESS_EQUAL;
        ThrowIfFailed(mDevice->CreateDepthStencilState(&depthDesc, mDynamicShadowDepthState.GetAddressOf()));
        depthDesc.DepthEnable = FALSE;
        depthDesc.DepthWriteMask = D3D11_DEPTH_WRITE_MASK_ZERO;
        depthDesc.DepthFunc = D3D11_COMPARISON_ALWAYS;
        ThrowIfFailed(mDevice->CreateDepthStencilState(&depthDesc, mDynamicShadowResolveDepthState.GetAddressOf()));

        D3D11_RASTERIZER_DESC rasterDesc = {};
        rasterDesc.FillMode = D3D11_FILL_SOLID;
        rasterDesc.CullMode = D3D11_CULL_NONE;
        rasterDesc.DepthClipEnable = TRUE;
        rasterDesc.DepthBias = 80;
        rasterDesc.SlopeScaledDepthBias = 1.5f;
        ThrowIfFailed(mDevice->CreateRasterizerState(&rasterDesc, mDynamicShadowRasterState.GetAddressOf()));
        rasterDesc.DepthBias = 0;
        rasterDesc.SlopeScaledDepthBias = 0.0f;
        rasterDesc.ScissorEnable = FALSE;
        ThrowIfFailed(mDevice->CreateRasterizerState(&rasterDesc, mDynamicShadowResolveRasterState.GetAddressOf()));

        D3D11_BLEND_DESC blendDesc = {};
        blendDesc.RenderTarget[0].BlendEnable = TRUE;
        blendDesc.RenderTarget[0].SrcBlend = D3D11_BLEND_SRC_ALPHA;
        blendDesc.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_ALPHA;
        blendDesc.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD;
        blendDesc.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ZERO;
        blendDesc.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_ONE;
        blendDesc.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD;
        blendDesc.RenderTarget[0].RenderTargetWriteMask = D3D11_COLOR_WRITE_ENABLE_ALL;
        ThrowIfFailed(mDevice->CreateBlendState(&blendDesc, mDynamicShadowBlendState.GetAddressOf()));

        D3D11_SAMPLER_DESC samplerDesc = {};
        samplerDesc.Filter = D3D11_FILTER_COMPARISON_MIN_MAG_LINEAR_MIP_POINT;
        samplerDesc.AddressU = samplerDesc.AddressV = samplerDesc.AddressW = D3D11_TEXTURE_ADDRESS_BORDER;
        samplerDesc.BorderColor[0] = samplerDesc.BorderColor[1] = samplerDesc.BorderColor[2] = samplerDesc.BorderColor[3] = 1.0f;
        samplerDesc.ComparisonFunc = D3D11_COMPARISON_LESS_EQUAL;
        samplerDesc.MinLOD = 0.0f;
        samplerDesc.MaxLOD = D3D11_FLOAT32_MAX;
        ThrowIfFailed(mDevice->CreateSamplerState(&samplerDesc, mDynamicShadowComparisonSampler.GetAddressOf()));
    }

    if (mDynamicShadowResolution != resolution || !mDynamicShadowTexture) {
        mDynamicShadowTexture.Reset();
        mDynamicShadowDsv.Reset();
        mDynamicShadowSrv.Reset();
        D3D11_TEXTURE2D_DESC textureDesc = {};
        textureDesc.Width = resolution;
        textureDesc.Height = resolution;
        textureDesc.MipLevels = 1;
        textureDesc.ArraySize = 1;
        textureDesc.Format = DXGI_FORMAT_R32_TYPELESS;
        textureDesc.SampleDesc.Count = 1;
        textureDesc.Usage = D3D11_USAGE_DEFAULT;
        textureDesc.BindFlags = D3D11_BIND_DEPTH_STENCIL | D3D11_BIND_SHADER_RESOURCE;
        ThrowIfFailed(mDevice->CreateTexture2D(&textureDesc, nullptr, mDynamicShadowTexture.GetAddressOf()));
        D3D11_DEPTH_STENCIL_VIEW_DESC dsvDesc = {};
        dsvDesc.Format = DXGI_FORMAT_D32_FLOAT;
        dsvDesc.ViewDimension = D3D11_DSV_DIMENSION_TEXTURE2D;
        ThrowIfFailed(mDevice->CreateDepthStencilView(mDynamicShadowTexture.Get(), &dsvDesc,
                                                       mDynamicShadowDsv.GetAddressOf()));
        D3D11_SHADER_RESOURCE_VIEW_DESC srvDesc = {};
        srvDesc.Format = DXGI_FORMAT_R32_FLOAT;
        srvDesc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
        srvDesc.Texture2D.MipLevels = 1;
        ThrowIfFailed(mDevice->CreateShaderResourceView(mDynamicShadowTexture.Get(), &srvDesc,
                                                         mDynamicShadowSrv.GetAddressOf()));
        mDynamicShadowResolution = resolution;
    }

    if (vertexBytes > mDynamicShadowVertexBufferBytes) {
        mDynamicShadowVertexBuffer.Reset();
        size_t capacity = 64 * 1024;
        while (capacity < vertexBytes) {
            capacity *= 2;
        }
        D3D11_BUFFER_DESC vbDesc = {};
        vbDesc.ByteWidth = (UINT)capacity;
        vbDesc.Usage = D3D11_USAGE_DYNAMIC;
        vbDesc.BindFlags = D3D11_BIND_VERTEX_BUFFER;
        vbDesc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        ThrowIfFailed(mDevice->CreateBuffer(&vbDesc, nullptr, mDynamicShadowVertexBuffer.GetAddressOf()));
        mDynamicShadowVertexBufferBytes = capacity;
    }
}

void GfxRenderingAPIDX11::RenderDynamicShadowMap(const float* worldVertices, size_t vertexCount,
                                                  const float* cameraWorldToClip, const float lightDirection[3],
                                                  uint32_t resolution, float opacity, float bias, int pcfRadius) {
    FramebufferDX11& framebuffer = mFrameBuffers[mCurrentFramebuffer];
    if (worldVertices == nullptr || vertexCount < 3 || opacity <= 0.0f || cameraWorldToClip == nullptr ||
        framebuffer.depth_stencil_srv == nullptr) {
        return;
    }

    const size_t vertexBytes = vertexCount * 3 * sizeof(float);
    EnsureDynamicShadowResources(std::max<uint32_t>(64, resolution), vertexBytes);

    float inverseCamera[16];
    if (!InvertShadowMatrix4x4(cameraWorldToClip, inverseCamera)) {
        return;
    }

    float dir[3] = { lightDirection[0], lightDirection[1], lightDirection[2] };
    NormalizeShadowVec(dir);
    float forward[3] = { -dir[0], -dir[1], -dir[2] };
    float upSeed[3] = { 0.0f, 1.0f, 0.0f };
    if (fabsf(forward[1]) > 0.95f) {
        upSeed[0] = 1.0f;
        upSeed[1] = 0.0f;
    }
    float right[3], up[3];
    CrossShadowVec(upSeed, forward, right);
    NormalizeShadowVec(right);
    CrossShadowVec(forward, right, up);
    NormalizeShadowVec(up);

    float minX = FLT_MAX, minY = FLT_MAX, minZ = FLT_MAX;
    float maxX = -FLT_MAX, maxY = -FLT_MAX, maxZ = -FLT_MAX;
    for (size_t i = 0; i < vertexCount; i++) {
        const float p[3] = { worldVertices[i * 3 + 0], worldVertices[i * 3 + 1], worldVertices[i * 3 + 2] };
        const float lx = DotShadowVec(p, right);
        const float ly = DotShadowVec(p, up);
        const float lz = DotShadowVec(p, forward);
        minX = std::min(minX, lx); maxX = std::max(maxX, lx);
        minY = std::min(minY, ly); maxY = std::max(maxY, ly);
        minZ = std::min(minZ, lz); maxZ = std::max(maxZ, lz);
    }

    float centerX = (minX + maxX) * 0.5f;
    float centerY = (minY + maxY) * 0.5f;
    float halfX = std::max(256.0f, (maxX - minX) * 0.5f + 64.0f);
    float halfY = std::max(256.0f, (maxY - minY) * 0.5f + 64.0f);
    // Stabilize the orthographic projection by snapping its center to whole shadow texels.
    const float texelX = (halfX * 2.0f) / (float)mDynamicShadowResolution;
    const float texelY = (halfY * 2.0f) / (float)mDynamicShadowResolution;
    centerX = floorf(centerX / texelX + 0.5f) * texelX;
    centerY = floorf(centerY / texelY + 0.5f) * texelY;
    minZ -= 256.0f;
    maxZ += 512.0f;
    const float depthRange = std::max(1.0f, maxZ - minZ);

    float lightViewProj[16] = {};
    lightViewProj[0] = right[0] / halfX;
    lightViewProj[4] = right[1] / halfX;
    lightViewProj[8] = right[2] / halfX;
    lightViewProj[12] = -centerX / halfX;
    lightViewProj[1] = up[0] / halfY;
    lightViewProj[5] = up[1] / halfY;
    lightViewProj[9] = up[2] / halfY;
    lightViewProj[13] = -centerY / halfY;
    lightViewProj[2] = forward[0] / depthRange;
    lightViewProj[6] = forward[1] / depthRange;
    lightViewProj[10] = forward[2] / depthRange;
    lightViewProj[14] = -minZ / depthRange;
    lightViewProj[15] = 1.0f;

    memcpy(mDynamicShadowCbData.lightViewProj, lightViewProj, sizeof(lightViewProj));
    memcpy(mDynamicShadowCbData.inverseCamera, inverseCamera, sizeof(inverseCamera));
    mDynamicShadowCbData.params[0] = std::clamp(opacity, 0.0f, 1.0f);
    mDynamicShadowCbData.params[1] = std::max(0.0f, bias);
    mDynamicShadowCbData.params[2] = 1.0f / (float)mDynamicShadowResolution;
    mDynamicShadowCbData.params[3] = (float)std::clamp(pcfRadius, 0, 2);

    D3D11_MAPPED_SUBRESOURCE mapped = {};
    ThrowIfFailed(mContext->Map(mDynamicShadowCb.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped));
    memcpy(mapped.pData, &mDynamicShadowCbData, sizeof(mDynamicShadowCbData));
    mContext->Unmap(mDynamicShadowCb.Get(), 0);
    ThrowIfFailed(mContext->Map(mDynamicShadowVertexBuffer.Get(), 0, D3D11_MAP_WRITE_DISCARD, 0, &mapped));
    memcpy(mapped.pData, worldVertices, vertexBytes);
    mContext->Unmap(mDynamicShadowVertexBuffer.Get(), 0);

    ComPtr<ID3D11RenderTargetView> savedRtv;
    ComPtr<ID3D11DepthStencilView> savedDsv;
    mContext->OMGetRenderTargets(1, savedRtv.GetAddressOf(), savedDsv.GetAddressOf());
    D3D11_VIEWPORT savedViewports[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE];
    UINT savedViewportCount = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;
    mContext->RSGetViewports(&savedViewportCount, savedViewports);
    D3D11_RECT savedScissors[D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE];
    UINT savedScissorCount = D3D11_VIEWPORT_AND_SCISSORRECT_OBJECT_COUNT_PER_PIPELINE;
    mContext->RSGetScissorRects(&savedScissorCount, savedScissors);
    ComPtr<ID3D11RasterizerState> savedRaster;
    mContext->RSGetState(savedRaster.GetAddressOf());
    ComPtr<ID3D11DepthStencilState> savedDepthState;
    UINT savedStencilRef = 0;
    mContext->OMGetDepthStencilState(savedDepthState.GetAddressOf(), &savedStencilRef);
    ComPtr<ID3D11BlendState> savedBlend;
    FLOAT savedBlendFactor[4] = {};
    UINT savedSampleMask = 0;
    mContext->OMGetBlendState(savedBlend.GetAddressOf(), savedBlendFactor, &savedSampleMask);
    ComPtr<ID3D11InputLayout> savedLayout;
    mContext->IAGetInputLayout(savedLayout.GetAddressOf());
    ComPtr<ID3D11Buffer> savedVertexBuffer;
    UINT savedStride = 0, savedOffset = 0;
    mContext->IAGetVertexBuffers(0, 1, savedVertexBuffer.GetAddressOf(), &savedStride, &savedOffset);
    D3D11_PRIMITIVE_TOPOLOGY savedTopology;
    mContext->IAGetPrimitiveTopology(&savedTopology);
    ComPtr<ID3D11VertexShader> savedVs;
    ComPtr<ID3D11PixelShader> savedPs;
    mContext->VSGetShader(savedVs.GetAddressOf(), nullptr, nullptr);
    mContext->PSGetShader(savedPs.GetAddressOf(), nullptr, nullptr);
    ComPtr<ID3D11Buffer> savedVsCb;
    ComPtr<ID3D11Buffer> savedPsCb;
    mContext->VSGetConstantBuffers(0, 1, savedVsCb.GetAddressOf());
    mContext->PSGetConstantBuffers(0, 1, savedPsCb.GetAddressOf());
    ComPtr<ID3D11ShaderResourceView> savedSrvs[2];
    ID3D11ShaderResourceView* rawSavedSrvs[2] = {};
    mContext->PSGetShaderResources(0, 2, rawSavedSrvs);
    savedSrvs[0].Attach(rawSavedSrvs[0]);
    savedSrvs[1].Attach(rawSavedSrvs[1]);
    ComPtr<ID3D11SamplerState> savedSamplers[2];
    ID3D11SamplerState* rawSavedSamplers[2] = {};
    mContext->PSGetSamplers(0, 2, rawSavedSamplers);
    savedSamplers[0].Attach(rawSavedSamplers[0]);
    savedSamplers[1].Attach(rawSavedSamplers[1]);

    ID3D11ShaderResourceView* nullSrvs[2] = { nullptr, nullptr };
    mContext->PSSetShaderResources(0, 2, nullSrvs);

    // Pass 1: low-resolution directional depth map.
    D3D11_VIEWPORT shadowViewport = { 0.0f, 0.0f, (float)mDynamicShadowResolution,
                                     (float)mDynamicShadowResolution, 0.0f, 1.0f };
    D3D11_RECT shadowScissor = { 0, 0, (LONG)mDynamicShadowResolution, (LONG)mDynamicShadowResolution };
    mContext->OMSetRenderTargets(0, nullptr, mDynamicShadowDsv.Get());
    mContext->ClearDepthStencilView(mDynamicShadowDsv.Get(), D3D11_CLEAR_DEPTH, 1.0f, 0);
    mContext->RSSetViewports(1, &shadowViewport);
    mContext->RSSetScissorRects(1, &shadowScissor);
    mContext->RSSetState(mDynamicShadowRasterState.Get());
    mContext->OMSetDepthStencilState(mDynamicShadowDepthState.Get(), 0);
    mContext->OMSetBlendState(nullptr, nullptr, 0xFFFFFFFF);
    UINT shadowStride = sizeof(float) * 3;
    UINT shadowOffset = 0;
    ID3D11Buffer* shadowVb = mDynamicShadowVertexBuffer.Get();
    mContext->IASetInputLayout(mDynamicShadowInputLayout.Get());
    mContext->IASetVertexBuffers(0, 1, &shadowVb, &shadowStride, &shadowOffset);
    mContext->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    mContext->VSSetShader(mDynamicShadowDepthVs.Get(), nullptr, 0);
    mContext->PSSetShader(nullptr, nullptr, 0);
    ID3D11Buffer* shadowCb = mDynamicShadowCb.Get();
    mContext->VSSetConstantBuffers(0, 1, &shadowCb);
    mContext->Draw((UINT)vertexCount, 0);

    // Pass 2: deferred fullscreen resolve. The room depth reconstructs world position; the 3x3 PCF
    // comparison determines visibility from the light. Actors are drawn after this pass, so there is
    // no self-shadow and no actor-on-actor overdraw, matching the old optimized behavior.
    ID3D11RenderTargetView* resolveRtv = savedRtv.Get();
    mContext->OMSetRenderTargets(1, &resolveRtv, nullptr);
    if (savedViewportCount > 0) mContext->RSSetViewports(savedViewportCount, savedViewports);
    if (savedScissorCount > 0) mContext->RSSetScissorRects(savedScissorCount, savedScissors);
    mContext->RSSetState(mDynamicShadowResolveRasterState.Get());
    mContext->OMSetDepthStencilState(mDynamicShadowResolveDepthState.Get(), 0);
    const FLOAT blendFactor[4] = { 0, 0, 0, 0 };
    mContext->OMSetBlendState(mDynamicShadowBlendState.Get(), blendFactor, 0xFFFFFFFF);
    mContext->IASetInputLayout(nullptr);
    ID3D11Buffer* nullVb = nullptr;
    UINT zero = 0;
    mContext->IASetVertexBuffers(0, 1, &nullVb, &zero, &zero);
    mContext->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    mContext->VSSetShader(mDynamicShadowResolveVs.Get(), nullptr, 0);
    mContext->PSSetShader(framebuffer.msaa_level > 1 ? mDynamicShadowResolvePsMsaa.Get()
                                                     : mDynamicShadowResolvePs.Get(), nullptr, 0);
    mContext->PSSetConstantBuffers(0, 1, &shadowCb);
    ID3D11ShaderResourceView* resolveSrvs[2] = { framebuffer.depth_stencil_srv.Get(), mDynamicShadowSrv.Get() };
    mContext->PSSetShaderResources(0, 2, resolveSrvs);
    ID3D11SamplerState* comparisonSampler = mDynamicShadowComparisonSampler.Get();
    mContext->PSSetSamplers(1, 1, &comparisonSampler);
    mContext->Draw(3, 0);
    mContext->PSSetShaderResources(0, 2, nullSrvs);

    // Restore the exact Fast3D state so the following actor pass can continue without an artificial flush.
    ID3D11RenderTargetView* restoreRtv = savedRtv.Get();
    mContext->OMSetRenderTargets(1, &restoreRtv, savedDsv.Get());
    if (savedViewportCount > 0) mContext->RSSetViewports(savedViewportCount, savedViewports);
    if (savedScissorCount > 0) mContext->RSSetScissorRects(savedScissorCount, savedScissors);
    mContext->RSSetState(savedRaster.Get());
    mContext->OMSetDepthStencilState(savedDepthState.Get(), savedStencilRef);
    mContext->OMSetBlendState(savedBlend.Get(), savedBlendFactor, savedSampleMask);
    ID3D11Buffer* restoreVb = savedVertexBuffer.Get();
    mContext->IASetInputLayout(savedLayout.Get());
    mContext->IASetVertexBuffers(0, 1, &restoreVb, &savedStride, &savedOffset);
    mContext->IASetPrimitiveTopology(savedTopology);
    mContext->VSSetShader(savedVs.Get(), nullptr, 0);
    mContext->PSSetShader(savedPs.Get(), nullptr, 0);
    ID3D11Buffer* restoreVsCb = savedVsCb.Get();
    ID3D11Buffer* restorePsCb = savedPsCb.Get();
    mContext->VSSetConstantBuffers(0, 1, &restoreVsCb);
    mContext->PSSetConstantBuffers(0, 1, &restorePsCb);
    ID3D11ShaderResourceView* restoreSrvs[2] = { savedSrvs[0].Get(), savedSrvs[1].Get() };
    ID3D11SamplerState* restoreSamplers[2] = { savedSamplers[0].Get(), savedSamplers[1].Get() };
    mContext->PSSetShaderResources(0, 2, restoreSrvs);
    mContext->PSSetSamplers(0, 2, restoreSamplers);
}
'''

text = replace_once(
    text,
    'GfxRenderingAPIDX11::~GfxRenderingAPIDX11() {\n}\n',
    implementation + '\nGfxRenderingAPIDX11::~GfxRenderingAPIDX11() {\n}\n',
    "insert dx11 shadow implementation",
)

text = replace_once(
    text,
    '''    if (has_depth_buffer &&
        (diff || !fb.has_depth_buffer || (fb.depth_stencil_srv.Get() != nullptr) != can_extract_depth)) {
        fb.depth_stencil_srv.Reset();
        CreateDepthStencilObjects(width, height, msaa_level, fb.depth_stencil_view.ReleaseAndGetAddressOf(),
                                  can_extract_depth ? fb.depth_stencil_srv.GetAddressOf() : nullptr);
    }
''',
    '''    // The default framebuffer depth is also sampled by the deferred dynamic-shadow resolve.
    const bool needDepthSrv = can_extract_depth || fb_id == 0;
    if (has_depth_buffer &&
        (diff || !fb.has_depth_buffer || (fb.depth_stencil_srv.Get() != nullptr) != needDepthSrv)) {
        fb.depth_stencil_srv.Reset();
        CreateDepthStencilObjects(width, height, msaa_level, fb.depth_stencil_view.ReleaseAndGetAddressOf(),
                                  needDepthSrv ? fb.depth_stencil_srv.GetAddressOf() : nullptr);
    }
''',
    "ensure main depth srv",
)
path.write_text(text, encoding="utf-8")

print("Dynamic shadow map source patch applied successfully.")
