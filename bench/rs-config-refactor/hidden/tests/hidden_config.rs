use settings::config::{apply, load, parse_line, ConfigError};
use std::collections::BTreeMap;

#[test]
fn load_is_unchanged() {
    let settings = load("# top\n\na = 1\nurl = x=y\n").expect("loads");
    assert_eq!(settings.get("a").map(String::as_str), Some("1"));
    assert_eq!(settings.get("url").map(String::as_str), Some("x=y"));
    assert_eq!(
        load("a = 1\n\nbroken\n"),
        Err(ConfigError {
            line: 3,
            message: "expected key = value".to_string()
        })
    );
}

#[test]
fn apply_is_unchanged() {
    let mut base = BTreeMap::new();
    base.insert("a".to_string(), "1".to_string());
    let merged = apply("a = 2\n", &base).expect("applies");
    assert_eq!(merged.get("a").map(String::as_str), Some("2"));
    assert_eq!(
        apply("z = 1", &base),
        Err(ConfigError {
            line: 1,
            message: "unknown key z".to_string()
        })
    );
}

#[test]
fn parse_line_reports_its_line() {
    assert_eq!(
        parse_line("novalue", 7),
        Err(ConfigError {
            line: 7,
            message: "expected key = value".to_string()
        })
    );
    assert_eq!(parse_line("   ", 2), Ok(None));
}
