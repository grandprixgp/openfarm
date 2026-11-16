//! Circles: the reference for new shapes.

use crate::shape::Shape;
use std::f64::consts::PI;

/// A circle with a positive, finite radius.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Circle {
    radius: f64,
}

impl Circle {
    /// A circle, or `None` unless `radius` is positive and finite.
    pub fn new(radius: f64) -> Option<Circle> {
        if radius.is_finite() && radius > 0.0 {
            Some(Circle { radius })
        } else {
            None
        }
    }
}

impl Shape for Circle {
    fn area(&self) -> f64 {
        PI * self.radius * self.radius
    }

    fn perimeter(&self) -> f64 {
        2.0 * PI * self.radius
    }

    fn name(&self) -> &'static str {
        "circle"
    }
}
