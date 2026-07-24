from pathlib import Path

source_path = Path("tools/apply_world_shadow_casters.py")
source = source_path.read_text(encoding="utf-8")
start = source.index("for path in [")
end = source.index("\nregex_once(", start)
replacement = r'''for path in [
    "include/fast/backends/gfx_rendering_api.h",
    "include/fast/backends/gfx_direct3d_common.h",
]:
    regex_once(
        path,
        r"const float\* cameraWorldToClip, const float lightDir\[3\],\s+uint32_t resolution, float opacity, float bias, int pcfRadius\)",
        "const float* cameraWorldToClip, const float lightDir[3], const float shadowAnchor[3],\n"
        "                                        uint32_t resolution, float opacity, float bias, int pcfRadius)",
    )
'''
source = source[:start] + replacement + source[end:]
exec(compile(source, str(source_path), "exec"), {"__name__": "__main__"})
