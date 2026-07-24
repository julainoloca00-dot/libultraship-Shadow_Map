from pathlib import Path
import re


def read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8", newline="\n")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    if new in text:
        print(f"{path}: already patched")
        return
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected 1 exact match, found {count}")
    write(path, text.replace(old, new, 1))
    print(f"{path}: patched")


def regex_once(path: str, pattern: str, replacement: str) -> None:
    text = read(path)
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{path}: expected 1 regex match, found {count}")
    write(path, new)
    print(f"{path}: patched")


for path in [
    "include/fast/backends/gfx_rendering_api.h",
    "include/fast/backends/gfx_direct3d_common.h",
]:
    replace_once(
        path,
        """const float* cameraWorldToClip, const float lightDir[3],
                                        uint32_t resolution, float opacity, float bias, int pcfRadius)""",
        """const float* cameraWorldToClip, const float lightDir[3], const float shadowAnchor[3],
                                        uint32_t resolution, float opacity, float bias, int pcfRadius)""",
    )

regex_once(
    "include/fast/backends/gfx_direct3d_common.h",
    r"\n    // Persistent world/light-space placement prevents.*?float mDynamicShadowLastDirection\[3\] = \{ 0\.0f, 1\.0f, 0\.0f \};\n",
    "\n",
)

replace_once(
    "include/fast/interpreter.h",
    """    }
    void StartFrame();""",
    """    }
    void SetDynamicShadowCaptureState(bool enabled, const float lightDir[3], const float anchor[3]) {
        mDynamicShadowsEnabled = enabled;
        if (lightDir != nullptr) {
            mDynamicShadowLightDir[0] = lightDir[0];
            mDynamicShadowLightDir[1] = lightDir[1];
            mDynamicShadowLightDir[2] = lightDir[2];
        }
        if (anchor != nullptr) {
            mDynamicShadowAnchor[0] = anchor[0];
            mDynamicShadowAnchor[1] = anchor[1];
            mDynamicShadowAnchor[2] = anchor[2];
        }
        if (!enabled) {
            mCaptureEnvironmentShadow = false;
            mEnvironmentShadowCasterAccum.clear();
            mShadowCasterAccum.clear();
            mShadowVerts.clear();
        }
    }
    void StartFrame();""",
)

regex_once(
    "include/fast/interpreter.h",
    r"    std::vector<float> mShadowCasterAccum;\n    float mShadowLightDirAccum\[3\] = \{ 0\.0f, 0\.0f, 0\.0f \};\n    uint32_t mShadowLightDirSamples = 0;",
    """    std::vector<float> mShadowCasterAccum;
    // Visible opaque environment triangles captured before the actor pass. This reuses the normal
    // Fast3D traversal, so the room is not submitted a second time merely to build the shadow map.
    std::vector<float> mEnvironmentShadowCasterAccum;
    bool mDynamicShadowsEnabled = false;
    bool mCaptureEnvironmentShadow = false;
    float mDynamicShadowLightDir[3] = { 0.30f, 1.0f, 0.20f };
    float mDynamicShadowAnchor[3] = { 0.0f, 0.0f, 0.0f };
    static constexpr size_t kEnvironmentShadowBudgetFloats = 2u * 1024u * 1024u;""",
)

cpp = "src/fast/interpreter.cpp"
replace_once(
    cpp,
    """void Interpreter::StartFrame() {
    mWapi->GetDimensions""",
    """void Interpreter::StartFrame() {
    mCaptureEnvironmentShadow = mDynamicShadowsEnabled;
    mEnvironmentShadowCasterAccum.clear();

    mWapi->GetDimensions""",
)

replace_once(
    cpp,
    """        x = AdjXForAspectRatio(x);""",
    """        if (mCaptureEnvironmentShadow) {
            float(*shadowMv)[4] = mRsp->modelview_matrix_stack[mRsp->modelview_matrix_stack_size - 1];
            d->wx = v->ob[0] * shadowMv[0][0] + v->ob[1] * shadowMv[1][0] + v->ob[2] * shadowMv[2][0] +
                    shadowMv[3][0];
            d->wy = v->ob[0] * shadowMv[0][1] + v->ob[1] * shadowMv[1][1] + v->ob[2] * shadowMv[2][1] +
                    shadowMv[3][1];
            d->wz = v->ob[0] * shadowMv[0][2] + v->ob[1] * shadowMv[1][2] + v->ob[2] * shadowMv[2][2] +
                    shadowMv[3][2];
        }

        x = AdjXForAspectRatio(x);""",
)

replace_once(
    cpp,
    """    if (use_alpha) {
        cc_options |= SHADER_OPT(ALPHA);
    }""",
    """    const uint32_t cycleType = mRdp->other_mode_h & (3U << G_MDSFT_CYCLETYPE);
    const bool opaqueEnvironmentCaster =
        mCaptureEnvironmentShadow && !mFbActive && !is_rect && depth_test && depth_mask && !use_alpha &&
        !texture_edge && !alpha_threshold && !invisible && cycleType != G_CYC_COPY && cycleType != G_CYC_FILL;
    if (opaqueEnvironmentCaster &&
        mEnvironmentShadowCasterAccum.size() + 9 <= kEnvironmentShadowBudgetFloats) {
        for (int si = 0; si < 3; si++) {
            mEnvironmentShadowCasterAccum.push_back(v_arr[si]->wx);
            mEnvironmentShadowCasterAccum.push_back(v_arr[si]->wy);
            mEnvironmentShadowCasterAccum.push_back(v_arr[si]->wz);
        }
    }

    if (use_alpha) {
        cc_options |= SHADER_OPT(ALPHA);
    }""",
)

regex_once(
    cpp,
    r"\n        // A single optimized directional map uses the average dominant key of all accepted casters\..*?mShadowLightDirSamples\+\+;\n        \}\n",
    "\n",
)

regex_once(
    cpp,
    r"void Interpreter::RenderShadowVolumes\(\) \{.*?\n\}\n\nvoid Interpreter::GfxDpSetGrayscaleColor",
    """void Interpreter::RenderShadowVolumes() {
    // This command is emitted after the opaque room and before normal actors. Stop room capture here,
    // then combine this frame's visible environment with the previous frame's accepted actor casters.
    mCaptureEnvironmentShadow = false;

    const size_t available =
        mEnvironmentShadowCasterAccum.size() < kShadowAccumBudgetFloats
            ? kShadowAccumBudgetFloats - mEnvironmentShadowCasterAccum.size()
            : 0;
    const size_t copyFloats = std::min(mShadowCasterAccum.size(), available - (available % 9));
    if (copyFloats >= 9) {
        mEnvironmentShadowCasterAccum.insert(mEnvironmentShadowCasterAccum.end(), mShadowCasterAccum.begin(),
                                             mShadowCasterAccum.begin() + copyFloats);
    }

    // Fast3D applies its widescreen correction to clip X after P_matrix. Pass the exact effective
    // matrix to the backend so reconstruction from the DX11 depth buffer cannot swim with camera rotation.
    float effectiveCamera[16];
    memcpy(effectiveCamera, &mRsp->P_matrix[0][0], sizeof(effectiveCamera));
    if (!mFbActive && mCurDimensions.width > 0 && mCurDimensions.height > 0) {
        const float targetAspect = static_cast<float>(mCurDimensions.width) /
                                   static_cast<float>(mCurDimensions.height);
        const float aspectScale = (4.0f / 3.0f) / targetAspect;
        for (int row = 0; row < 4; row++) {
            effectiveCamera[row * 4] *= aspectScale;
        }
    }

    const size_t vertexCount = mEnvironmentShadowCasterAccum.size() / 3;
    mRapi->RenderDynamicShadowMap(vertexCount >= 3 ? mEnvironmentShadowCasterAccum.data() : nullptr, vertexCount,
                                  effectiveCamera, mDynamicShadowLightDir, mDynamicShadowAnchor,
                                  kDynamicShadowMapResolution, std::clamp(mToonShadowAlpha, 0.0f, 1.0f),
                                  kDynamicShadowMapBias, kDynamicShadowMapPcfRadius);

    mEnvironmentShadowCasterAccum.clear();
    mShadowCasterAccum.clear();

    for (int band = 0; band < kShadowBands; band++) {
        mShadowVolumeAccum[band].clear();
        mShadowVolumeKind[band].clear();
    }
}

void Interpreter::GfxDpSetGrayscaleColor""",
)

inc = "src/fast/backends/gfx_direct3d11_shadow_map.inc"
replace_once(
    inc,
    """const float* cameraWorldToClip, const float lightDirection[3],
                                                  uint32_t resolution, float opacity, float bias, int pcfRadius)""",
    """const float* cameraWorldToClip, const float lightDirection[3], const float shadowAnchor[3],
                                                  uint32_t resolution, float opacity, float bias, int pcfRadius)""",
)
replace_once(
    inc,
    """if (worldVertices == nullptr || vertexCount < 3 || opacity <= 0.0f || cameraWorldToClip == nullptr ||
        framebuffer.depth_stencil_srv == nullptr)""",
    """if (worldVertices == nullptr || vertexCount < 3 || opacity <= 0.0f || cameraWorldToClip == nullptr ||
        shadowAnchor == nullptr || framebuffer.depth_stencil_srv == nullptr)""",
)
regex_once(
    inc,
    r"    // Fast3D applies its widescreen correction to clip X.*?if \(!InvertShadowMatrix4x4\(effectiveCamera, inverseCamera\)\) \{\n        return;\n    \}",
    """    float inverseCamera[16];
    if (!InvertShadowMatrix4x4(cameraWorldToClip, inverseCamera)) {
        return;
    }""",
)
regex_once(
    inc,
    r"    float minX = FLT_MAX;.*?const float depthRange = std::max\(1\.0f, maxZ - minZ\);",
    """    static constexpr float kShadowWorldHalfExtent = 2048.0f;
    static constexpr float kShadowWorldDepthHalfExtent = 4096.0f;
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
    const float depthRange = kShadowWorldDepthHalfExtent * 2.0f;""",
)
