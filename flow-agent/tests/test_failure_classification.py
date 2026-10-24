"""Offline error classification; no provider requests or generation retries."""
import pytest

from flow_agent.console import safe_failure
from flow_agent.engine import EngineError

def test_unknown_image_result_is_not_reported_as_page_control_failure():
    from flow_agent.official_images import OfficialImageError
    message = safe_failure(OfficialImageError('original_result_unknown', submitted=True))
    assert '待核对' in message and '180秒' in message
    assert '控件' not in message


def error(stdout="", stderr="", code=1):
    return EngineError(["flow.exe", "image", "PRIVATE_PROMPT", "--cookies", "PRIVATE_PATH"], code, stdout, stderr)


def test_empty_result_is_not_misreported_as_captcha_failure():
    message = safe_failure(error('{"status":"empty","media":[]}', "recaptcha strategy: auto; captcha probe available"))
    assert "EMPTY_RESULT" in message
    assert "具体原因" in message
    assert "CAPTCHA_FAILED" not in message


def test_normal_captcha_logs_alone_do_not_prove_failure():
    message = safe_failure(error(stderr="recaptcha strategy: auto; token minted; request ended without details"))
    assert "CAPTCHA_FAILED" not in message
    assert "PRIVATE" not in message


@pytest.mark.parametrize("details", ["captcha failed", "recaptcha token rejected", "captcha timed out", "captcha unavailable"])
def test_explicit_verification_failure_has_clear_next_step(details):
    message = safe_failure(error(stderr=details))
    assert "CAPTCHA_FAILED" in message
    assert "本人完成" in message


def test_upstream_unusual_activity_has_priority_over_generic_empty_result():
    message = safe_failure(error('{"status":"empty"}', "captcha: PUBLIC_ERROR_UNUSUAL_ACTIVITY"))
    assert "异常活动" in message
    assert "EMPTY_RESULT" not in message


def test_named_upstream_refusal_is_not_hidden_by_empty_status():
    message = safe_failure(error('{"status":"empty"}', 'server refused: PUBLIC_ERROR_PERMISSION_DENIED Cookie=PRIVATE'))
    assert 'PUBLIC_ERROR_PERMISSION_DENIED' in message
    assert 'EMPTY_RESULT' not in message and 'PRIVATE' not in message


@pytest.mark.parametrize("details", ["unknown model", "invalid model", "unsupported model"])
def test_model_rejection_is_distinct_from_verification(details):
    assert "MODEL_UNAVAILABLE" in safe_failure(error(stderr=details))


def test_private_provider_content_is_never_returned():
    message = safe_failure(error(stderr="Cookie=PRIVATE_COOKIE Token=PRIVATE_TOKEN captcha configured"))
    assert "PRIVATE" not in message


def test_engine_timeout_preserves_uncertain_billing_warning():
    assert "可能仍在处理" in safe_failure(error(code=-1))


def test_official_execution_protection_has_priority_and_hides_private_details():
    message = safe_failure(error(stderr='BROWSER_VERIFICATION_PROTECTED captcha unavailable Cookie=PRIVATE_TOKEN'))
    assert 'BROWSER_VERIFICATION_PROTECTED' in message
    assert '未获取验证令牌、未提交生成' in message
    assert '官方页面' in message
    assert 'CAPTCHA_FAILED' not in message and 'PRIVATE' not in message


def test_real_native_empty_stderr_without_json_is_not_captcha_failure():
    text = ('recaptcha: token acquired via http\n'
            'batchexecute: the retry came back empty too\n'
            'engine: the transport accepted the image request and returned no image')
    assert 'EMPTY_RESULT' in safe_failure(error(stderr=text))
    assert 'CAPTCHA_FAILED' not in safe_failure(error(stderr=text))
