//! Shapes with straight sides.

use crate::shape::Shape;

/// A square with a positive, finite side.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Square {
    side: f64,
}

impl Square {
    /// A square, or `None` unless `side` is positive and finite.
    pub fn new(side: f64) -> Option<Square> {
        if side.is_finite() && side > 0.0 {
            Some(Square { side })
        } else {
            None
        }
    }
}

impl Shape for Square {
    fn area(&self) -> f64 {
        self.side * self.side
    }

    fn perimeter(&self) -> f64 {
        4.0 * self.side
    }

    fn name(&self) -> &'static str {
        "square"
    }
}
