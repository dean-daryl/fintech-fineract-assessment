from app.seed import seed_accounts


def test_seed_covers_the_brief_examples_with_unique_codes_and_references() -> None:
    accounts = seed_accounts()
    codes = [account["code"] for account in accounts]
    assert len(set(codes)) == len(codes) == 9
    assert {"1100", "1200", "4100"} <= set(codes)
    assert all(account["external_reference"] == f"GL-{account['code']}" and account["currency"] == "RWF" for account in accounts)
