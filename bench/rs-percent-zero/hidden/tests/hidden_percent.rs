use stats::percent::percent;

#[test]
fn ordinary_values_round_down() {
    assert_eq!(percent(1, 3), 33);
    assert_eq!(percent(50, 200), 25);
    assert_eq!(percent(0, 0), 0);
}

#[test]
fn huge_values_do_not_overflow() {
    assert_eq!(percent(u64::MAX, u64::MAX), 100);
    assert_eq!(percent(u64::MAX / 2, u64::MAX), 49);
}

#[test]
fn a_result_too_large_saturates() {
    assert_eq!(percent(u64::MAX, 1), u64::MAX);
}
