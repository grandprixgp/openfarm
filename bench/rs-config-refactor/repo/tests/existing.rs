use settings::config::load;

#[test]
fn one_setting() {
    let settings = load("a = 1").expect("loads");
    assert_eq!(settings.len(), 1);
}
