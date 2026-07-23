from pathlib import Path
import re


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


header = Path("include/fast/backends/gfx_direct3d_common.h")
text = header.read_text(encoding="utf-8")

text = replace_once(
    text,
    "struct PerDrawCB {\n",
    """struct DynamicShadowCB {
    float lightViewProj[16];
    float inverseCamera[16];
    // x = opacity, y = receiver bias, z = inverse map resolution, w = PCF radius
    float params[4];
};

struct PerDrawCB {
""",
    "constant buffer declaration",
)

text = replace_once(
    text,
    "    void SetSrgbMode() override;\n",
    """    void SetSrgbMode() override;
    void RenderDynamicShadowMap(const float* worldVertices, size_t vertexCount,
                                const float* cameraWorldToClip, const float lightDir[3],
                                uint32_t resolution, float opacity, float bias, int pcfRadius) override;
""",
    "public renderer hook",
)

text = replace_once(
    text,
    "    HMODULE mDX11Module;\n",
    """    void EnsureDynamicShadowResources(uint32_t resolution, size_t vertexBytes);

    HMODULE mDX11Module;
""",
    "private helper declaration",
)

text = replace_once(
    text,
    "    Microsoft::WRL::ComPtr<ID3D11Buffer> mCoordBuffer;\n",
    """    // SOH [Enhancement] 512x512 directional shadow depth + deferred PCF resolve.
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
""",
    "DX11 resource members",
)
header.write_text(text, encoding="utf-8")


source = Path("src/fast/backends/gfx_direct3d11.cpp")
text = source.read_text(encoding="utf-8")
if "#include <algorithm>\n" not in text:
    text = replace_once(text, "#include <cfloat>\n", "#include <cfloat>\n#include <algorithm>\n", "algorithm include")
text = replace_once(
    text,
    "namespace Fast {\n",
    "namespace Fast {\n\n#include \"gfx_direct3d11_shadow_map.inc\"\n",
    "shadow implementation include",
)

old_pattern = re.compile(
    r"    if \(has_depth_buffer &&\n"
    r"        \(diff \|\| !fb\.has_depth_buffer \|\| \(fb\.depth_stencil_srv\.Get\(\) != nullptr\) != can_extract_depth\)\) \{\n"
    r"        fb\.depth_stencil_srv\.Reset\(\);\n"
    r"        CreateDepthStencilObjects\(width, height, msaa_level, fb\.depth_stencil_view\.ReleaseAndGetAddressOf\(\),\n"
    r"                                  can_extract_depth \? fb\.depth_stencil_srv\.GetAddressOf\(\) : nullptr\);\n"
    r"    \}\n"
)
replacement = """    // The deferred PCF resolve samples the default framebuffer depth even when callers do not request extraction.
    const bool needDepthSrv = can_extract_depth || fb_id == 0;
    if (has_depth_buffer &&
        (diff || !fb.has_depth_buffer || (fb.depth_stencil_srv.Get() != nullptr) != needDepthSrv)) {
        fb.depth_stencil_srv.Reset();
        CreateDepthStencilObjects(width, height, msaa_level, fb.depth_stencil_view.ReleaseAndGetAddressOf(),
                                  needDepthSrv ? fb.depth_stencil_srv.GetAddressOf() : nullptr);
    }
"""
text, count = old_pattern.subn(replacement, text, count=1)
if count != 1:
    raise RuntimeError(f"default depth SRV block: expected one match, found {count}")
source.write_text(text, encoding="utf-8")

print("Strict DX11 dynamic-shadow backend patch applied.")
