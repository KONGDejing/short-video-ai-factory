"""The daily short-video allocation approved by the account owner."""

THREE_PLATFORM_PLAN = 'three-platform-v1'
PLATFORM_LIMITS = {'douyin': 4, 'kuaishou': 2, 'xiaohongshu': 1}
ADDITIONAL_TIMES = {'kuaishou': ('07:20', '09:00'), 'xiaohongshu': ('08:10',)}
DAILY_OUTPUT = sum(PLATFORM_LIMITS.values())
AI_DISCLOSURE = '内容由AI生成（AI辅助文案、配音与剪辑）'


def full_video_plan(config):
    """A full daily video plan uses the quotas of the enabled video destinations."""
    enabled = set(config.platforms) & PLATFORM_LIMITS.keys()
    return bool(enabled - {'douyin'}) and config.daily_target == sum(PLATFORM_LIMITS[p] for p in enabled)


def scheduled_times(config):
    clocks = [(clock, 'douyin') for clock in config.publish_times]
    if full_video_plan(config):
        for platform, values in ADDITIONAL_TIMES.items():
            if platform in config.platforms:
                clocks.extend((clock, platform) for clock in values)
    return sorted(clocks)
