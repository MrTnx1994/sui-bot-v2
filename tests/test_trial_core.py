"""تست هستهٔ اکانت تست رایگان (trial_core)."""

from __future__ import annotations


from sui_bot.trial_core import TRIAL_DAYS, TRIAL_GB, TrialStore, trial_expiry_ts, trial_volume_bytes


def test_trial_constants():
    assert TRIAL_GB == 0.5                    # ۵۰۰ مگابایت
    assert TRIAL_DAYS == 1
    assert trial_volume_bytes() == 536_870_912  # 0.5 GiB
    assert trial_expiry_ts() > 0


def test_once_per_user(tmp_path):
    store = TrialStore(tmp_path / "trial_users.json")
    assert not store.has_used(111)
    store.record(111, "tabcd1234")
    assert store.has_used(111)
    assert not store.has_used(222)
    assert store.count() == 1


def test_persistence(tmp_path):
    path = tmp_path / "trial_users.json"
    TrialStore(path).record(999, "txyz9999")
    reopened = TrialStore(path)
    assert reopened.has_used(999)
