//! A fixed-size ring of recent readings.

/// Keeps the last `capacity` readings.
#[derive(Debug)]
pub struct Ring {
    capacity: usize,
    items: Vec<i64>,
}

impl Ring {
    /// Make an empty ring that keeps `capacity` readings.
    pub fn new(capacity: usize) -> Self {
        Ring {
            capacity,
            items: Vec::new(),
        }
    }

    /// Record a reading.
    pub fn push(&mut self, value: i64) {
        self.items.push(value);
    }

    /// The readings kept, oldest first.
    pub fn items(&self) -> &[i64] {
        &self.items
    }

    /// How many readings the ring keeps.
    pub fn capacity(&self) -> usize {
        self.capacity
    }
}
