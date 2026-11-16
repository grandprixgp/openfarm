use units::parse::parse_size;

#[test]
fn kilobytes() {
    assert_eq!(parse_size("10k"), Ok(10240));
}
