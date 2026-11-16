+++
id = "CARD-1"
title = "A triangle shape like the circle"
state = "ready"
category = "pattern"
unit = "geometry"
touches = ["src/polygon.rs"]
reference = "src/circle.rs"
new_symbols = true
depends = []

[[tests]]
name = "a_three_four_five_triangle_has_area_six"
file = "tests/triangle.rs"
asserts = "Triangle::new(3.0, 4.0, 5.0) is Some and its area() is within 1e-9 of 6.0"
+++

## Done

- New `pub struct Triangle` in `src/polygon.rs` with `pub fn new(a: f64, b: f64, c: f64) -> Option<Triangle>`, following `Circle` and `Square`.
- `new` returns `None` unless all three sides are finite and positive and each side is strictly less than the sum of the other two (so `(1, 2, 3)` is `None`).
- `Triangle` implements `Shape`: `area` by Heron's formula, `perimeter` as `a + b + c`, and `name` as `"triangle"`.
