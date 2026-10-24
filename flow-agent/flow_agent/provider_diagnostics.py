"""Allowlisted provider-error evidence. Never retain upstream free text."""
import re

REASONS = {
    'unusual_activity': ('官方提示异常活动', r'(?<![A-Za-z0-9_])(?:PUBLIC_ERROR_UNUSUAL_ACTIVITY|unusual activity)(?![A-Za-z0-9_])|异常活动'),
    'permission_denied': ('官方说明拒绝访问或权限不足', r'(?<![A-Za-z0-9_])(?:PERMISSION_DENIED|permission denied|access denied)(?![A-Za-z0-9_])|无权访问|权限不足'),
    'verification_required': ('官方要求登录或人工验证', r'(?<![A-Za-z0-9_])(?:CAPTCHA_FAILED|RECAPTCHA_FAILED|UNAUTHENTICATED|verification required|sign in required)(?![A-Za-z0-9_])|需要人工验证|请重新登录'),
    'quota_exhausted': ('官方说明积分或配额不足', r'(?<![A-Za-z0-9_])(?:INSUFFICIENT_CREDITS|RESOURCE_EXHAUSTED|insufficient credits|quota exceeded)(?![A-Za-z0-9_])|积分不足|配额不足'),
    'rate_limited': ('官方说明请求过于频繁', r'(?<![A-Za-z0-9_])(?:RATE_LIMIT_EXCEEDED|rate limit exceeded|too many requests)(?![A-Za-z0-9_])|请求过于频繁'),
    'invalid_argument': ('官方说明请求参数无效', r'(?<![A-Za-z0-9_])(?:INVALID_ARGUMENT|invalid argument)(?![A-Za-z0-9_])|参数无效'),
    'policy_rejected': ('官方说明内容未通过政策检查', r'(?<![A-Za-z0-9_])(?:PUBLIC_ERROR_SAFETY|SAFETY_FILTERED|content policy violation)(?![A-Za-z0-9_])|违反内容政策'),
    'model_unavailable': ('官方说明模型不可用', r'(?<![A-Za-z0-9_])(?:MODEL_UNAVAILABLE|MODEL_NOT_FOUND|model unavailable)(?![A-Za-z0-9_])|模型不可用'),
    'service_unavailable': ('官方说明服务暂时不可用', r'(?<![A-Za-z0-9_])(?:SERVICE_UNAVAILABLE|service unavailable|DEADLINE_EXCEEDED)(?![A-Za-z0-9_])|服务暂时不可用'),
}
ALLOWED_FIELDS = frozenset({'message','reason','status','code','details','error','errors','description','violations'})

def reason_codes(error):
    found = set()
    budget = [256]
    def visit(value, depth=0):
        if depth > 6 or budget[0] <= 0:
            return
        budget[0] -= 1
        if isinstance(value, str):
            # Opaque strings, raw bodies and URLs are never copied to output.
            if len(value) > 4096:
                return
            for code, (_, pattern) in REASONS.items():
                if re.search(pattern, value, re.I):
                    found.add(code)
        elif isinstance(value, list):
            for child in value[:32]: visit(child, depth+1)
        elif isinstance(value, dict):
            for key, child in list(value.items())[:32]:
                if key in ALLOWED_FIELDS: visit(child, depth+1)
    visit(error)
    return sorted(found)

def error_record(error):
    codes = reason_codes(error[1:])
    return {'code': error[0], 'reason_codes': codes,
            'classification_source': 'local_allowlist_match_on_rpc_error',
            'upstream_markers': upstream_markers(error[1:]),
            'reason_status': 'recognized' if codes else 'not_provided_or_unrecognized'}


def upstream_markers(value, depth=0):
    """Only fixed public markers; never copy provider text or credentials."""
    if depth > 6:
        return []
    if isinstance(value, str):
        return (['PUBLIC_ERROR_UNUSUAL_ACTIVITY'] if len(value) <= 4096 and
                re.search(r'(?<![A-Za-z0-9_])PUBLIC_ERROR_UNUSUAL_ACTIVITY(?![A-Za-z0-9_])', value) else [])
    if isinstance(value, list):
        children = value[:32]
    elif isinstance(value, dict):
        children = [v for k,v in list(value.items())[:32] if k in ALLOWED_FIELDS]
    else:
        return []
    return sorted({m for child in children for m in upstream_markers(child, depth+1)})

def public_diagnostic(summary):
    records = summary.get('rpc_errors', []) if isinstance(summary, dict) else []
    result = []
    for record in records[:4]:
        if not isinstance(record, dict) or type(record.get('code')) is not int:
            continue
        codes = record.get('reason_codes', [])
        labels = [REASONS[c][0] for c in codes if isinstance(c,str) and c in REASONS][:4] if isinstance(codes,list) else []
        result.append('生成响应错误码 '+str(record['code'])+'；程序分类：'+('；'.join(labels).replace('官方', '响应') if labels else '未提供可识别的具体说明，原因尚未确定')+'（不代表账号被封禁）')
    return '。'.join(result)
