// SOH [Enhancement] Optimized dynamic shadow mapping for the Windows/DX11 backend.
// Included from gfx_direct3d11.cpp inside namespace Fast.

namespace {

bool InvertShadowMatrix4x4(const float* src, float* dst) {
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

void NormalizeShadowVec(float v[3]) {
    float len2 = v[0] * v[0] + v[1] * v[1] + v[2] * v[2];
    if (len2 < 1e-8f) {
        v[0] = 0.30f;
        v[1] = 1.0f;
        v[2] = 0.20f;
        len2 = v[0] * v[0] + v[1] * v[1] + v[2] * v[2];
    }
    const float invLen = 1.0f / sqrtf(len2);
    v[0] *= invLen;
    v[1] *= invLen;
    v[2] *= invLen;
}

void CrossShadowVec(const float a[3], const float b[3], float out[3]) {
    out[0] = a[1] * b[2] - a[2] * b[1];
    out[1] = a[2] * b[0] - a[0] * b[2];
    out[2] = a[0] * b[1] - a[1] * b[0];
}

float DotShadowVec(const float a[3], const float b[3]) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

} // namespace

void GfxRenderingAPIDX11::EnsureDynamicShadowResources(uint32_t resolution, size_t vertexBytes) {
    const UINT compileFlags = D3DCOMPILE_OPTIMIZATION_LEVEL2;
    auto compile = [&](const char* source, const char* entry, const char* target, ID3DBlob** blob) {
        ComPtr<ID3DBlob> errors;
        const HRESULT hr = mD3dCompile(source, strlen(source), nullptr, nullptr, nullptr, entry, target, compileFlags,
                                       0, blob, errors.GetAddressOf());
        if (FAILED(hr)) {
            const char* errorText =
                errors ? static_cast<const char*>(errors->GetBufferPointer()) : "Unknown dynamic shadow shader error";
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
    float4 localLight;
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
    float4 localLight;
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
    uint width, height;
    sceneDepth.GetDimensions(width, height);
    int2 pixel = int2(min(input.uv * float2(width, height), float2(width - 1, height - 1)));
    float depth = sceneDepth.Load(int3(pixel, 0));
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
    float edgeDistance = min(min(shadowUv.x, 1.0 - shadowUv.x), min(shadowUv.y, 1.0 - shadowUv.y));
    float edgeFade = saturate(edgeDistance * 64.0);

    int radius = clamp((int)shadowParams.w, 0, 2);
    float lit = 0.0;
    float samples = 0.0;
    [unroll] for (int y = -2; y <= 2; y++) {
        [unroll] for (int x = -2; x <= 2; x++) {
            if (abs(x) <= radius && abs(y) <= radius) {
                float weight = (x == 0 && y == 0) ? 4.0 : ((x == 0 || y == 0) ? 2.0 : 1.0);
                lit += shadowDepth.SampleCmpLevelZero(shadowComparison,
                    shadowUv + float2(x, y) * shadowParams.z, lightNdc.z - shadowParams.y) * weight;
                samples += weight;
            }
        }
    }
    const float rawVisibility = lit / max(samples, 1.0);
    const float thresholdWidth = max(fwidth(rawVisibility) * 1.5, 0.025);
    const float visibility = smoothstep(0.5 - thresholdWidth, 0.5 + thresholdWidth, rawVisibility);
    const float shadow = 1.0 - visibility;
    float localFill = 0.0;
    if (localLight.w > 0.0) {
        localFill = saturate(1.0 - length(world.xyz - localLight.xyz) / localLight.w);
        localFill = localFill * localFill * (3.0 - 2.0 * localFill);
    }
    const float localShadowScale = 1.0 - localFill * 0.85;
    return float4(0.0, 0.0, 0.0,
                  saturate(shadowParams.x * shadow * edgeFade * localShadowScale));
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
    float4 localLight;
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
    float edgeDistance = min(min(shadowUv.x, 1.0 - shadowUv.x), min(shadowUv.y, 1.0 - shadowUv.y));
    float edgeFade = saturate(edgeDistance * 64.0);

    int radius = clamp((int)shadowParams.w, 0, 2);
    float lit = 0.0;
    float samples = 0.0;
    [unroll] for (int y = -2; y <= 2; y++) {
        [unroll] for (int x = -2; x <= 2; x++) {
            if (abs(x) <= radius && abs(y) <= radius) {
                float weight = (x == 0 && y == 0) ? 4.0 : ((x == 0 || y == 0) ? 2.0 : 1.0);
                lit += shadowDepth.SampleCmpLevelZero(shadowComparison,
                    shadowUv + float2(x, y) * shadowParams.z, lightNdc.z - shadowParams.y) * weight;
                samples += weight;
            }
        }
    }
    const float rawVisibility = lit / max(samples, 1.0);
    const float thresholdWidth = max(fwidth(rawVisibility) * 1.5, 0.025);
    const float visibility = smoothstep(0.5 - thresholdWidth, 0.5 + thresholdWidth, rawVisibility);
    const float shadow = 1.0 - visibility;
    float localFill = 0.0;
    if (localLight.w > 0.0) {
        localFill = saturate(1.0 - length(world.xyz - localLight.xyz) / localLight.w);
        localFill = localFill * localFill * (3.0 - 2.0 * localFill);
    }
    const float localShadowScale = 1.0 - localFill * 0.85;
    return float4(0.0, 0.0, 0.0,
                  saturate(shadowParams.x * shadow * edgeFade * localShadowScale));
}
)SHADOW";

        ComPtr<ID3DBlob> depthVsBlob;
        ComPtr<ID3DBlob> resolveVsBlob;
        ComPtr<ID3DBlob> resolvePsBlob;
        ComPtr<ID3DBlob> resolvePsMsaaBlob;
        compile(depthShader, "VSMain", "vs_4_0", depthVsBlob.GetAddressOf());
        compile(resolveShader, "VSMain", "vs_4_0", resolveVsBlob.GetAddressOf());
        compile(resolveShader, "PSMain", "ps_4_0", resolvePsBlob.GetAddressOf());
        compile(resolveShaderMsaa, "PSMain", "ps_4_1", resolvePsMsaaBlob.GetAddressOf());

        ThrowIfFailed(mDevice->CreateVertexShader(depthVsBlob->GetBufferPointer(), depthVsBlob->GetBufferSize(),
                                                  nullptr, mDynamicShadowDepthVs.GetAddressOf()));
        ThrowIfFailed(mDevice->CreateVertexShader(resolveVsBlob->GetBufferPointer(), resolveVsBlob->GetBufferSize(),
                                                  nullptr, mDynamicShadowResolveVs.GetAddressOf()));
        ThrowIfFailed(mDevice->CreatePixelShader(resolvePsBlob->GetBufferPointer(), resolvePsBlob->GetBufferSize(),
                                                 nullptr, mDynamicShadowResolvePs.GetAddressOf()));
        ThrowIfFailed(mDevice->CreatePixelShader(resolvePsMsaaBlob->GetBufferPointer(),
                                                 resolvePsMsaaBlob->GetBufferSize(), nullptr,
                                                 mDynamicShadowResolvePsMsaa.GetAddressOf()));

        D3D11_INPUT_ELEMENT_DESC input = { "POSITION", 0, DXGI_FORMAT_R32G32B32_FLOAT,
                                           0,          0, D3D11_INPUT_PER_VERTEX_DATA,
                                           0 };
        ThrowIfFailed(mDevice->CreateInputLayout(&input, 1, depthVsBlob->GetBufferPointer(),
                                                 depthVsBlob->GetBufferSize(),
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
        rasterDesc.DepthBias = 48;
        rasterDesc.SlopeScaledDepthBias = 1.0f;
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
        // Point comparisons plus a weighted 3x3 kernel preserve the silhouette; hardware linear
        // comparison filtering blurred each tap again and made a 512 map look much softer than intended.
        samplerDesc.Filter = D3D11_FILTER_COMPARISON_MIN_MAG_MIP_POINT;
        samplerDesc.AddressU = D3D11_TEXTURE_ADDRESS_BORDER;
        samplerDesc.AddressV = D3D11_TEXTURE_ADDRESS_BORDER;
        samplerDesc.AddressW = D3D11_TEXTURE_ADDRESS_BORDER;
        samplerDesc.BorderColor[0] = 1.0f;
        samplerDesc.BorderColor[1] = 1.0f;
        samplerDesc.BorderColor[2] = 1.0f;
        samplerDesc.BorderColor[3] = 1.0f;
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
        ThrowIfFailed(
            mDevice->CreateDepthStencilView(mDynamicShadowTexture.Get(), &dsvDesc, mDynamicShadowDsv.GetAddressOf()));

        D3D11_SHADER_RESOURCE_VIEW_DESC srvDesc = {};
        srvDesc.Format = DXGI_FORMAT_R32_FLOAT;
        srvDesc.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
        srvDesc.Texture2D.MipLevels = 1;
        ThrowIfFailed(
            mDevice->CreateShaderResourceView(mDynamicShadowTexture.Get(), &srvDesc, mDynamicShadowSrv.GetAddressOf()));
        mDynamicShadowResolution = resolution;
    }

    if (vertexBytes > mDynamicShadowVertexBufferBytes) {
        mDynamicShadowVertexBuffer.Reset();
        size_t capacity = 64 * 1024;
        while (capacity < vertexBytes) {
            capacity *= 2;
        }

        D3D11_BUFFER_DESC vbDesc = {};
        vbDesc.ByteWidth = static_cast<UINT>(capacity);
        vbDesc.Usage = D3D11_USAGE_DYNAMIC;
        vbDesc.BindFlags = D3D11_BIND_VERTEX_BUFFER;
        vbDesc.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE;
        ThrowIfFailed(mDevice->CreateBuffer(&vbDesc, nullptr, mDynamicShadowVertexBuffer.GetAddressOf()));
        mDynamicShadowVertexBufferBytes = capacity;
    }
}

void GfxRenderingAPIDX11::RenderDynamicShadowMap(const float* worldVertices, size_t vertexCount,
                                                 const float* cameraWorldToClip, const float lightDirection[3],
                                                 const float shadowAnchor[3], const float localLight[4],
                                                 uint32_t resolution, float opacity, float bias, int pcfRadius) {
    FramebufferDX11& framebuffer = mFrameBuffers[mCurrentFramebuffer];
    if (worldVertices == nullptr || vertexCount < 3 || opacity <= 0.0f || cameraWorldToClip == nullptr ||
        shadowAnchor == nullptr || framebuffer.depth_stencil_srv == nullptr) {
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

    // Build a continuous light-space basis from the horizontal light direction. The old 0.95 threshold
    // abruptly changed the up seed near vertical light and could rotate the whole shadow map in one frame.
    float right[3] = { forward[2], 0.0f, -forward[0] };
    float horizontalLenSq = right[0] * right[0] + right[2] * right[2];
    if (horizontalLenSq < 1e-8f) {
        right[0] = 1.0f;
        right[1] = 0.0f;
        right[2] = 0.0f;
    } else {
        const float invHorizontalLen = 1.0f / sqrtf(horizontalLenSq);
        right[0] *= invHorizontalLen;
        right[2] *= invHorizontalLen;
    }
    float up[3];
    CrossShadowVec(forward, right, up);
    NormalizeShadowVec(up);

    // Ultra-quality near cascade: 4096 texels over 2048 world units gives 0.5 world units per texel.
    // This intentionally prioritizes a Breath-of-the-Wild-like crisp silhouette over GPU cost.
    static constexpr float kShadowWorldHalfExtent = 1024.0f;
    static constexpr float kShadowWorldDepthHalfExtent = 3072.0f;
    const float halfX = kShadowWorldHalfExtent;
    const float halfY = kShadowWorldHalfExtent;
    const float texelX = (halfX * 2.0f) / static_cast<float>(mDynamicShadowResolution);
    const float texelY = (halfY * 2.0f) / static_cast<float>(mDynamicShadowResolution);

    const float anchorCenterX = DotShadowVec(shadowAnchor, right);
    const float anchorCenterY = DotShadowVec(shadowAnchor, up);
    const float centerX = floorf(anchorCenterX / texelX + 0.5f) * texelX;
    const float centerY = floorf(anchorCenterY / texelY + 0.5f) * texelY;
    const float anchorCenterZ = DotShadowVec(shadowAnchor, forward);
    const float minZ = anchorCenterZ - kShadowWorldDepthHalfExtent;
    const float depthRange = kShadowWorldDepthHalfExtent * 2.0f;

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
    mDynamicShadowCbData.params[2] = 1.0f / static_cast<float>(mDynamicShadowResolution);
    mDynamicShadowCbData.params[3] = static_cast<float>(std::clamp(pcfRadius, 0, 2));
    if (localLight != nullptr) {
        memcpy(mDynamicShadowCbData.localLight, localLight, sizeof(mDynamicShadowCbData.localLight));
    } else {
        memset(mDynamicShadowCbData.localLight, 0, sizeof(mDynamicShadowCbData.localLight));
    }

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
    UINT savedStride = 0;
    UINT savedOffset = 0;
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

    // Pass 1: rasterize only the accepted camera-visible casters into a 512x512 depth texture.
    const D3D11_VIEWPORT shadowViewport = {
        0.0f, 0.0f, static_cast<float>(mDynamicShadowResolution), static_cast<float>(mDynamicShadowResolution),
        0.0f, 1.0f
    };
    const D3D11_RECT shadowScissor = { 0, 0, static_cast<LONG>(mDynamicShadowResolution),
                                       static_cast<LONG>(mDynamicShadowResolution) };
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
    mContext->Draw(static_cast<UINT>(vertexCount), 0);

    // Pass 2: reconstruct world position from the room depth and resolve a 3x3 PCF shadow mask.
    ID3D11RenderTargetView* resolveRtv = savedRtv.Get();
    mContext->OMSetRenderTargets(1, &resolveRtv, nullptr);
    if (savedViewportCount > 0) {
        mContext->RSSetViewports(savedViewportCount, savedViewports);
    }
    if (savedScissorCount > 0) {
        mContext->RSSetScissorRects(savedScissorCount, savedScissors);
    }
    mContext->RSSetState(mDynamicShadowResolveRasterState.Get());
    mContext->OMSetDepthStencilState(mDynamicShadowResolveDepthState.Get(), 0);
    const FLOAT blendFactor[4] = { 0.0f, 0.0f, 0.0f, 0.0f };
    mContext->OMSetBlendState(mDynamicShadowBlendState.Get(), blendFactor, 0xFFFFFFFF);

    mContext->IASetInputLayout(nullptr);
    ID3D11Buffer* nullVb = nullptr;
    UINT zero = 0;
    mContext->IASetVertexBuffers(0, 1, &nullVb, &zero, &zero);
    mContext->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
    mContext->VSSetShader(mDynamicShadowResolveVs.Get(), nullptr, 0);
    mContext->PSSetShader(
        framebuffer.msaa_level > 1 ? mDynamicShadowResolvePsMsaa.Get() : mDynamicShadowResolvePs.Get(), nullptr, 0);
    mContext->PSSetConstantBuffers(0, 1, &shadowCb);
    ID3D11ShaderResourceView* resolveSrvs[2] = { framebuffer.depth_stencil_srv.Get(), mDynamicShadowSrv.Get() };
    mContext->PSSetShaderResources(0, 2, resolveSrvs);
    ID3D11SamplerState* comparisonSampler = mDynamicShadowComparisonSampler.Get();
    mContext->PSSetSamplers(1, 1, &comparisonSampler);
    mContext->Draw(3, 0);
    mContext->PSSetShaderResources(0, 2, nullSrvs);

    // Restore the exact Fast3D state before the actor pass continues.
    ID3D11RenderTargetView* restoreRtv = savedRtv.Get();
    mContext->OMSetRenderTargets(1, &restoreRtv, savedDsv.Get());
    if (savedViewportCount > 0) {
        mContext->RSSetViewports(savedViewportCount, savedViewports);
    }
    if (savedScissorCount > 0) {
        mContext->RSSetScissorRects(savedScissorCount, savedScissors);
    }
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
