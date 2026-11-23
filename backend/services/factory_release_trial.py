"""A live validation requested by the account owner can gate later submissions."""
from models.factory import FactoryPublication, FactoryRelease, FactoryWorkOrder


def trial_key(platform):
    return f'platform-release-trial:{platform}'


def require_live_trial(db, platform, item_id):
    if platform not in {'xiaohongshu', 'kuaishou'}:
        return
    order = db.get(FactoryWorkOrder, trial_key(platform))
    if not order or order.kind != 'platform_release_trial' or order.status != 'validating':
        return
    release_id = (order.evidence or {}).get('release_id')
    slot = db.get(FactoryRelease, release_id) if release_id else None
    if slot and slot.platforms == [platform] and slot.item_id == item_id:
        return
    if slot and slot.platforms == [platform] and slot.item_id:
        receipt = db.query(FactoryPublication).filter_by(item_id=slot.item_id, platform=platform,
                                                        action='publish', status='submitted').one_or_none()
        if receipt:
            from services.factory_publishing import _valid_evidence
            if _valid_evidence(platform, (receipt.result or {}).get('evidence', {})):
                order.evidence = {**order.evidence, 'receipt_job_id': receipt.id}
                order.status = 'completed'
                return
    raise ValueError('等待本次试发取得平台官方回执，后续作品按计划制作并保留。')
