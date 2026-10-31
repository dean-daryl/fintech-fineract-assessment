from app.faults import FaultInjector, FaultPoint


def test_disabled_injector_never_fires() -> None:
    faults = FaultInjector(enabled=False)
    faults.arm(FaultPoint.FINERACT_DOWN, "REPAY-1", 3)
    assert not faults.fires(FaultPoint.FINERACT_DOWN, "REPAY-1")


def test_fault_fires_only_for_its_point_and_reference_and_only_n_times() -> None:
    faults = FaultInjector(enabled=True)
    faults.arm(FaultPoint.FINERACT_DOWN, "REPAY-1", 2)
    assert not faults.fires(FaultPoint.FINERACT_DOWN, "REPAY-2")
    assert not faults.fires(FaultPoint.ERP_TIMEOUT_AFTER_WRITE, "REPAY-1")
    assert [faults.fires(FaultPoint.FINERACT_DOWN, "REPAY-1") for _ in range(3)] == [True, True, False]
    assert faults.armed() == []


def test_arming_again_replaces_and_clear_removes_everything() -> None:
    faults = FaultInjector(enabled=True)
    faults.arm(FaultPoint.FINERACT_DOWN, "REPAY-1", 5)
    faults.arm(FaultPoint.FINERACT_DOWN, "REPAY-1", 1)
    faults.arm(FaultPoint.CRASH_BEFORE_ERP, "REPAY-2", 1)
    assert [(f["point"], f["remaining"]) for f in faults.armed()] == [("FINERACT_DOWN", 1), ("CRASH_BEFORE_ERP", 1)]
    faults.clear()
    assert faults.armed() == []


def test_crash_runs_only_when_armed() -> None:
    crashes: list[str] = []
    faults = FaultInjector(enabled=True, crash=lambda: crashes.append("crash"))
    faults.crash_if_armed("REPAY-1")
    faults.arm(FaultPoint.CRASH_BEFORE_ERP, "REPAY-1", 1)
    faults.crash_if_armed("REPAY-1")
    faults.crash_if_armed("REPAY-1")
    assert crashes == ["crash"]
