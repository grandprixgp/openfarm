use stats::percent::percent;

#[test]
fn half_is_fifty() {
    assert_eq!(percent(1, 2), 50);
}
