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
        raise RuntimeError(f"{path}: expected one exact match, found {count}")
    write(path, text.replace(old, new, 1))
    print(f"{path}: patched")


def replace_count(path: str, old: str, new: str, expected: int) -> None:
    text = read(path)
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{path}: expected {expected} exact matches, found {count}")
    write(path, text.replace(old, new))
    print(f"{path}: patched {count} occurrences")


header = "include/fast/interpreter.h"
replace_once(
    header,
    '''    void SetDynamicShadowCaptureState(bool enabled, const float lightDir[3], const float anchor[3]) {
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
''',
    '''    void SetDynamicShadowCaptureState(bool enabled, const float lightDir[3], const float anchor[3]) {
        mDynamicShadowsEnabled = enabled;
        if (lightDir != nullptr) {
            float nextDir[3] = { lightDir[0], lightDir[1], lightDir[2] };
            float lenSq = nextDir[0] * nextDir[0] + nextDir[1] * nextDir[1] + nextDir[2] * nextDir[2];
            if (lenSq < 1e-8f) {
                nextDir[0] = 0.30f;
                nextDir[1] = 1.0f;
                nextDir[2] = 0.20f;
                lenSq = nextDir[0] * nextDir[0] + nextDir[1] * nextDir[1] + nextDir[2] * nextDir[2];
            }
            float invLen = 1.0f / sqrtf(lenSq);
            nextDir[0] *= invLen;
            nextDir[1] *= invLen;
            nextDir[2] *= invLen;

            // Shadow rays must come from above. Reuse the existing Length slider's minimum elevation so
            // a low sun or moon cannot create kilometer-long silhouettes across the room.
            if (nextDir[1] < 0.0f) {
                nextDir[0] = -nextDir[0];
                nextDir[1] = -nextDir[1];
                nextDir[2] = -nextDir[2];
            }
            float minElevation = mToonShadowMinElevation;
            if (minElevation < 0.35f) {
                minElevation = 0.35f;
            } else if (minElevation > 0.85f) {
                minElevation = 0.85f;
            }
            if (nextDir[1] < minElevation) {
                float horizontalLen = sqrtf(nextDir[0] * nextDir[0] + nextDir[2] * nextDir[2]);
                float horizontalTarget = sqrtf(1.0f - minElevation * minElevation);
                if (horizontalLen > 1e-5f) {
                    float horizontalScale = horizontalTarget / horizontalLen;
                    nextDir[0] *= horizontalScale;
                    nextDir[2] *= horizontalScale;
                } else {
                    nextDir[0] = horizontalTarget;
                    nextDir[2] = 0.0f;
                }
                nextDir[1] = minElevation;
            }

            if (!mDynamicShadowLightValid) {
                mDynamicShadowLightDir[0] = nextDir[0];
                mDynamicShadowLightDir[1] = nextDir[1];
                mDynamicShadowLightDir[2] = nextDir[2];
                mDynamicShadowLightValid = true;
                mDynamicShadowPendingFrames = 0;
            } else {
                float dot = mDynamicShadowLightDir[0] * nextDir[0] + mDynamicShadowLightDir[1] * nextDir[1] +
                            mDynamicShadowLightDir[2] * nextDir[2];
                if (dot < 0.5f) {
                    // At dawn/dusk the engine can alternate between sun and moon for a few frames. Require
                    // a large direction change to remain stable before accepting it, preventing full-map flashes.
                    float pendingDot = mDynamicShadowPendingDir[0] * nextDir[0] +
                                       mDynamicShadowPendingDir[1] * nextDir[1] +
                                       mDynamicShadowPendingDir[2] * nextDir[2];
                    if (mDynamicShadowPendingFrames == 0 || pendingDot < 0.98f) {
                        mDynamicShadowPendingDir[0] = nextDir[0];
                        mDynamicShadowPendingDir[1] = nextDir[1];
                        mDynamicShadowPendingDir[2] = nextDir[2];
                        mDynamicShadowPendingFrames = 1;
                    } else if (++mDynamicShadowPendingFrames >= 8) {
                        mDynamicShadowLightDir[0] = nextDir[0];
                        mDynamicShadowLightDir[1] = nextDir[1];
                        mDynamicShadowLightDir[2] = nextDir[2];
                        mDynamicShadowPendingFrames = 0;
                    }
                } else {
                    mDynamicShadowPendingFrames = 0;
                    constexpr float blend = 0.12f;
                    float filtered[3] = {
                        mDynamicShadowLightDir[0] + (nextDir[0] - mDynamicShadowLightDir[0]) * blend,
                        mDynamicShadowLightDir[1] + (nextDir[1] - mDynamicShadowLightDir[1]) * blend,
                        mDynamicShadowLightDir[2] + (nextDir[2] - mDynamicShadowLightDir[2]) * blend,
                    };
                    float filteredLenSq = filtered[0] * filtered[0] + filtered[1] * filtered[1] +
                                          filtered[2] * filtered[2];
                    if (filteredLenSq > 1e-8f) {
                        float filteredInvLen = 1.0f / sqrtf(filteredLenSq);
                        mDynamicShadowLightDir[0] = filtered[0] * filteredInvLen;
                        mDynamicShadowLightDir[1] = filtered[1] * filteredInvLen;
                        mDynamicShadowLightDir[2] = filtered[2] * filteredInvLen;
                    }
                }
            }
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
            mDynamicShadowLightValid = false;
            mDynamicShadowPendingFrames = 0;
        }
    }
''',
)

replace_once(
    header,
    '''    float mDynamicShadowLightDir[3] = { 0.30f, 1.0f, 0.20f };
    float mDynamicShadowAnchor[3] = { 0.0f, 0.0f, 0.0f };
    static constexpr size_t kEnvironmentShadowBudgetFloats = 2u * 1024u * 1024u;
    static constexpr uint32_t kDynamicShadowMapResolution = 512;
    static constexpr float kDynamicShadowMapBias = 0.0015f;
    static constexpr int kDynamicShadowMapPcfRadius = 1; // 3x3 Percentage-Closer Filtering
''',
    '''    float mDynamicShadowLightDir[3] = { 0.30f, 1.0f, 0.20f };
    float mDynamicShadowPendingDir[3] = { 0.30f, 1.0f, 0.20f };
    bool mDynamicShadowLightValid = false;
    uint8_t mDynamicShadowPendingFrames = 0;
    float mDynamicShadowAnchor[3] = { 0.0f, 0.0f, 0.0f };
    static constexpr size_t kEnvironmentShadowBudgetFloats = 2u * 1024u * 1024u;
    static constexpr float kEnvironmentShadowCaptureRadiusSq = 2304.0f * 2304.0f;
    static constexpr uint32_t kDynamicShadowMapResolution = 1024;
    static constexpr float kDynamicShadowMapBias = 0.0012f;
    static constexpr int kDynamicShadowMapPcfRadius = 1; // weighted 3x3 Percentage-Closer Filtering
''',
)

cpp = "src/fast/interpreter.cpp"
replace_once(
    cpp,
    '''    // if (rand()%2) return;

    if (v1->clip_rej & v2->clip_rej & v3->clip_rej) {
''',
    '''    // Capture a short guard volume before camera rejection. Nearby walls and props just outside the
    // viewport can still cast into the visible image, so dropping them at the exact screen edge made the
    // shadow blink while rotating the camera. Distance and opacity gates keep this far cheaper than capturing
    // the whole room, and the guard radius lies just outside the shadow-map footprint.
    if (mCaptureEnvironmentShadow && !mFbActive && !is_rect &&
        (v1->w > 0.0f || v2->w > 0.0f || v3->w > 0.0f) &&
        mEnvironmentShadowCasterAccum.size() + 9 <= kEnvironmentShadowBudgetFloats) {
        bool shadowDepthTest = (mRsp->geometry_mode & G_ZBUFFER) == G_ZBUFFER;
        bool shadowDepthMask = (mRdp->other_mode_l & Z_UPD) == Z_UPD;
        bool shadowUseAlpha =
            ((mRdp->other_mode_l & (3 << 20)) == (G_BL_CLR_MEM << 20) &&
             (mRdp->other_mode_l & (3 << 16)) == (G_BL_1MA << 16)) ||
            ((mRdp->other_mode_l & (3 << 22)) == (G_BL_CLR_MEM << 22) &&
             (mRdp->other_mode_l & (3 << 18)) == (G_BL_1MA << 18));
        bool shadowTextureEdge = (mRdp->other_mode_l & CVG_X_ALPHA) == CVG_X_ALPHA;
        bool shadowAlphaThreshold = (mRdp->other_mode_l & (3U << G_MDSFT_ALPHACOMPARE)) == G_AC_THRESHOLD;
        bool shadowInvisible = (mRdp->other_mode_l & (3 << 24)) == (G_BL_0 << 24) &&
                               (mRdp->other_mode_l & (3 << 20)) == (G_BL_CLR_MEM << 20);
        if (shadowTextureEdge) {
            if (shadowUseAlpha) {
                shadowAlphaThreshold = true;
                shadowTextureEdge = false;
            }
            shadowUseAlpha = true;
        }
        const uint32_t shadowCycleType = mRdp->other_mode_h & (3U << G_MDSFT_CYCLETYPE);
        const float centerX = (v1->wx + v2->wx + v3->wx) / 3.0f;
        const float centerY = (v1->wy + v2->wy + v3->wy) / 3.0f;
        const float centerZ = (v1->wz + v2->wz + v3->wz) / 3.0f;
        const float anchorDx = centerX - mDynamicShadowAnchor[0];
        const float anchorDy = centerY - mDynamicShadowAnchor[1];
        const float anchorDz = centerZ - mDynamicShadowAnchor[2];
        const bool insideShadowGuard =
            anchorDx * anchorDx + anchorDz * anchorDz <= kEnvironmentShadowCaptureRadiusSq &&
            fabsf(anchorDy) <= 3072.0f;
        const bool opaqueEnvironmentCaster =
            insideShadowGuard && shadowDepthTest && shadowDepthMask && !shadowUseAlpha && !shadowTextureEdge &&
            !shadowAlphaThreshold && !shadowInvisible && shadowCycleType != G_CYC_COPY &&
            shadowCycleType != G_CYC_FILL;
        if (opaqueEnvironmentCaster) {
            for (int si = 0; si < 3; si++) {
                mEnvironmentShadowCasterAccum.push_back(v_arr[si]->wx);
                mEnvironmentShadowCasterAccum.push_back(v_arr[si]->wy);
                mEnvironmentShadowCasterAccum.push_back(v_arr[si]->wz);
            }
        }
    }

    // if (rand()%2) return;

    if (v1->clip_rej & v2->clip_rej & v3->clip_rej) {
''',
)

replace_once(
    cpp,
    '''    const uint32_t cycleType = mRdp->other_mode_h & (3U << G_MDSFT_CYCLETYPE);
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

''',
    '',
)

inc = "src/fast/backends/gfx_direct3d11_shadow_map.inc"
replace_count(
    inc,
    '''    if (shadowUv.x <= 0.0 || shadowUv.x >= 1.0 || shadowUv.y <= 0.0 || shadowUv.y >= 1.0 ||
        lightNdc.z <= 0.0 || lightNdc.z >= 1.0) discard;

    int radius = clamp((int)shadowParams.w, 0, 2);
''',
    '''    if (shadowUv.x <= 0.0 || shadowUv.x >= 1.0 || shadowUv.y <= 0.0 || shadowUv.y >= 1.0 ||
        lightNdc.z <= 0.0 || lightNdc.z >= 1.0) discard;
    float edgeDistance = min(min(shadowUv.x, 1.0 - shadowUv.x), min(shadowUv.y, 1.0 - shadowUv.y));
    float edgeFade = saturate(edgeDistance * 64.0);

    int radius = clamp((int)shadowParams.w, 0, 2);
''',
    2,
)
replace_count(
    inc,
    '''                lit += shadowDepth.SampleCmpLevelZero(shadowComparison,
                    shadowUv + float2(x, y) * shadowParams.z, lightNdc.z - shadowParams.y);
                samples += 1.0;
''',
    '''                float weight = (x == 0 && y == 0) ? 4.0 : ((x == 0 || y == 0) ? 2.0 : 1.0);
                lit += shadowDepth.SampleCmpLevelZero(shadowComparison,
                    shadowUv + float2(x, y) * shadowParams.z, lightNdc.z - shadowParams.y) * weight;
                samples += weight;
''',
    2,
)
replace_count(
    inc,
    '''    return float4(0.0, 0.0, 0.0, saturate(shadowParams.x * shadow));
''',
    '''    return float4(0.0, 0.0, 0.0, saturate(shadowParams.x * shadow * edgeFade));
''',
    2,
)
replace_once(
    inc,
    '''        samplerDesc.Filter = D3D11_FILTER_COMPARISON_MIN_MAG_LINEAR_MIP_POINT;
''',
    '''        // Point comparisons plus a weighted 3x3 kernel preserve the silhouette; hardware linear
        // comparison filtering blurred each tap again and made a 512 map look much softer than intended.
        samplerDesc.Filter = D3D11_FILTER_COMPARISON_MIN_MAG_MIP_POINT;
''',
)
replace_once(
    inc,
    '''    float forward[3] = { -dir[0], -dir[1], -dir[2] };
    float upSeed[3] = { 0.0f, 1.0f, 0.0f };
    if (fabsf(forward[1]) > 0.95f) {
        upSeed[0] = 1.0f;
        upSeed[1] = 0.0f;
    }

    float right[3];
    float up[3];
    CrossShadowVec(upSeed, forward, right);
    NormalizeShadowVec(right);
    CrossShadowVec(forward, right, up);
    NormalizeShadowVec(up);

    static constexpr float kShadowWorldHalfExtent = 2048.0f;
    static constexpr float kShadowWorldDepthHalfExtent = 4096.0f;
''',
    '''    float forward[3] = { -dir[0], -dir[1], -dir[2] };

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

    // 1024x1024 over a tighter player-centred footprint gives ~3 world units per texel instead of 8.
    // The depth range still covers tall rooms while improving precision for walls and actor silhouettes.
    static constexpr float kShadowWorldHalfExtent = 1536.0f;
    static constexpr float kShadowWorldDepthHalfExtent = 3072.0f;
''',
)

print("Shadow stability and definition patch complete")
