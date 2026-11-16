//! A last-in, first-out stack.

/// A stack of values.
#[derive(Debug)]
pub struct Stack<T> {
    items: Vec<T>,
}

impl<T> Default for Stack<T> {
    fn default() -> Self {
        Self::new()
    }
}

impl<T> Stack<T> {
    /// Make an empty stack.
    pub fn new() -> Self {
        Stack { items: Vec::new() }
    }

    /// Put a value on top.
    pub fn push(&mut self, value: T) {
        self.items.push(value);
    }

    /// Take the top value off, if there is one.
    pub fn pop(&mut self) -> Option<T> {
        self.items.pop()
    }

    /// How many values are on the stack.
    pub fn len(&self) -> usize {
        self.items.len()
    }

    /// Whether the stack holds nothing.
    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }
}
