use geometry::circle::Circle;
use geometry::polygon::Square;
use geometry::shape::Shape;

#[test]
fn a_unit_square() {
    let square = Square::new(1.0).expect("a valid square");
    assert_eq!(square.area(), 1.0);
    assert_eq!(square.name(), "square");
    assert!(Circle::new(0.0).is_none());
}
