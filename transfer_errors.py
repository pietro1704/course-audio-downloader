"""Stable public failure codes; arbitrary exception messages stay private."""
import errno
import subprocess

KNOWN_FAILURES=frozenset("""
invalid-shared-document-permission
Safari-lesson-identity-timeout Safari-navigation-failed Safari-unavailable
ambiguous-document-identity-or-path ambiguous-external-media another-document-run-is-active
artwork-original-content-changed artwork-processing-failed artwork-reconciliation-incomplete
artwork-refresh-not-applicable artwork-verification-failed audio-encode-failed
catalogue-media-identity-mismatch catalogue-profile-origin-mismatch cleanup-hash-mismatch
cloud-copy-hash-mismatch cloud-destination-conflict cloud-document-hash-mismatch
cloud-exclusive-promotion-failed cloud-final-hash-mismatch cloud-receipt-metadata-mismatch
cloud-size-mismatch delivery-cancelled delivery-identity-mismatch disk-full disk-reserve
document-cancelled document-cleanup-hash-mismatch document-delivery-incomplete document-http-error
document-inventory-timeout document-is-media document-login-or-html-response
document-page-identity-timeout document-receipt-identity-mismatch document-size-budget-exceeded
document-size-mismatch document-source-hash-mismatch documents-require-explicit-browser-target
drm-media-not-supported duplicate-source-different-lesson embedded-lesson-identity-timeout
encrypted-or-invalid-manifest existing-final-validation-failed external-media-download-failed
external-media-extraction-failed external-media-identity-or-format-mismatch
external-output-already-exists external-source-budget-exceeded extractor-dependency-missing
hls-audio-download-failed hls-source-budget-exceeded icloud-bridge-unavailable icloud-eviction-failed
icloud-native-query-failed icloud-resource-query-failed icloud-upload-error-or-conflict
icloud-upload-not-confirmed icloud-upload-timeout incomplete-download incomplete-external-audio
incomplete-hls-audio insecure-document-redirect insecure-media insecure-player-template
invalid-artwork-recovery-state invalid-browser-target invalid-document-concurrency
invalid-document-format invalid-document-identity invalid-document-inventory invalid-document-page
invalid-document-receipt-state invalid-document-resource invalid-document-size invalid-document-url
invalid-next-data invalid-original-audio invalid-player-playlist invalid-player-segment-content
invalid-player-segment-duration invalid-player-segment-list invalid-player-variant
invalid-shared-source-permission invalid-site-path legacy-current-source-mismatch
legacy-identity-mismatch legacy-source-hash-mismatch lesson-identity-timeout lesson-pipeline-failed
local-final-hash-mismatch login-required media-download-failed missing-catalogue-page-identity
missing-document-download-url missing-document-filename missing-document-identity
missing-media-assets missing-next-data missing-player-hls missing-player-vod-audio
missing-prepared-document missing-profile-origin missing-variants no-audio-candidate
silence-profile-mismatch silence-dependency-missing silence-model-hash-mismatch
silence-inference-failed silence-codec-failed silence-invalid-pcm silence-pcm-budget-exceeded
silence-processing-timeout output-validation-failed overlapping-working-and-cloud-roots oversized-manifest
player-audio-download-failed player-download-timeout player-fetch-failed player-identity-mismatch
player-remux-failed player-response-too-large player-segment-fetch-failed player-segment-size-budget
player-segment-timeout player-source-duration-mismatch protected-media-not-supported
protected-or-unbounded-media repeated-document-inventory-failure source-size-budget-exceeded
timeout-expired unbounded-or-invalid-hls-duration unconfirmed-cloud-document
unexpected-complete-document-source unexpected-media-content-type unresolved-external-media
unsafe-document-path unsafe-player-media-url unsupported-lesson unsupported-media-host
unsupported-provider untracked-local-document untrusted-existing-final
""".split()) | frozenset(f'document-http-{code}' for code in range(400, 600))


def failure_reason(error):
    if isinstance(error,subprocess.TimeoutExpired):return 'timeout-expired'
    if isinstance(error,OSError) and error.errno==errno.ENOSPC:return 'disk-full'
    if isinstance(error,(RuntimeError,ValueError)) and str(error) in KNOWN_FAILURES:return str(error)
    return type(error).__name__
