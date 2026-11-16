//! What every shape can tell you.

/// A flat shape.
pub trait Shape {
    /// The area inside it.
    fn area(&self) -> f64;
    /// The length around it.
    fn perimeter(&self) -> f64;
    /// A lowercase name such as `"circle"`.
    fn name(&self) -> &'static str;
}
