use geometry::polygon::Triangle;
use geometry::shape::Shape;

fn close(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-9
}

#[test]
fn measures() {
    let t = Triangle::new(3.0, 4.0, 5.0).expect("a valid triangle");
    assert!(close(t.perimeter(), 12.0));
    assert_eq!(t.name(), "triangle");
    let equilateral = Triangle::new(2.0, 2.0, 2.0).expect("a valid triangle");
    assert!(close(equilateral.area(), 3.0_f64.sqrt()));
}

#[test]
fn impossible_sides_are_refused() {
    assert!(Triangle::new(1.0, 2.0, 3.0).is_none());
    assert!(Triangle::new(0.0, 1.0, 1.0).is_none());
    assert!(Triangle::new(-1.0, 2.0, 2.0).is_none());
    assert!(Triangle::new(f64::NAN, 2.0, 2.0).is_none());
    assert!(Triangle::new(f64::INFINITY, 2.0, 2.0).is_none());
    assert!(Triangle::new(1.0, 10.0, 1.0).is_none());
}
