from scripts.verify_migrations import verify


def test_legacy_upgrade_and_downgrade():
    verify()
