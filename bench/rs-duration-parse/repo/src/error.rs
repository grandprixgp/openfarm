//! The one error this crate returns.

/// Text that could not be read as a unit.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UnitError(pub String);
