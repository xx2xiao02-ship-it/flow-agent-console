# 0.2.58 临时自动调度策略

用户授权暂时跳过 account_5bce66c57ef3，优先使用 account_156fe4a60088。
仅影响新任务自动选号；手动指定账号、账号记录、会话和原任务恢复均保留。
成功账号不可用、积分不足或忙碌时使用其他符合条件的未暂停账号。
所有候选都被暂停时，不回退使用暂停账号。
策略保存在console_meta键automatic_account_policy，并通过GET /console/api/scheduler返回automatic_account_policy。
暂停不会因账号积分刷新自动解除；用户确认恢复后可以清除此临时策略。无需删除账号。
本次不向Google提交生成。测试只能确认调度逻辑，不代表官方风控已解除。
