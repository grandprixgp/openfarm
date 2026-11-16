use units::parse::parse_duration;

#[test]
fn every_suffix_and_none() {
    assert_eq!(parse_duration("90s"), Ok(90));
    assert_eq!(parse_duration("2h"), Ok(7200));
    assert_eq!(parse_duration("45"), Ok(45));
    assert_eq!(parse_duration("0m"), Ok(0));
}

#[test]
fn refusals() {
    for text in ["", "m", "-5s", "1.5h", "h5", "5 m"] {
        assert!(parse_duration(text).is_err(), "{text:?} should be refused");
    }
}

#[test]
fn overflow_is_refused() {
    assert!(parse_duration("18446744073709551615h").is_err());
    assert!(parse_duration("99999999999999999999").is_err());
}
