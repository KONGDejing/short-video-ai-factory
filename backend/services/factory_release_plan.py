"""The daily short-video allocation approved by the account owner."""

THREE_PLATFORM_PLAN = 'three-platform-v1'
PLATFORM_LIMITS = {'douyin': 4, 'kuaishou': 2, 'xiaohongshu': 1}
ADDITIONAL_TIMES = {'kuaishou': ('07:20', '09:00'), 'xiaohongshu': ('08:10',)}
DAILY_OUTPUT = sum(PLATFORM_LIMITS.values())
AI_DISCLOSURE = '内容由AI生成（AI辅助文案、配音与剪辑）'


def video_platform_limits(platforms):
    limits = {platform: count for platform, count in PLATFORM_LIMITS.items() if platform in platforms}
    if set(limits) == {'douyin', 'kuaishou'}:
        limits['kuaishou'] += PLATFORM_LIMITS['xiaohongshu']
    return limits


def video_times(platform, platforms):
    if platform not in platforms:
        return ()
    clocks = ADDITIONAL_TIMES.get(platform, ())
    if platform == 'kuaishou' and video_platform_limits(platforms)[platform] == 3:
        return (*clocks, '10:00')
    return clocks


def full_video_plan(config):
    """A full daily video plan uses the quotas of the enabled video destinations."""
    limits = video_platform_limits(config.platforms)
    return bool(limits.keys() - {'douyin'}) and config.daily_target == sum(limits.values())


def scheduled_times(config):
    clocks = [(clock, 'douyin') for clock in config.publish_times]
    if full_video_plan(config):
        for platform in ADDITIONAL_TIMES:
            clocks.extend((clock, platform) for clock in video_times(platform, config.platforms))
    return sorted(clocks)
