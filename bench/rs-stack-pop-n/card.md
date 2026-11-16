+++
id = "CARD-1"
title = "Pop several values at once"
state = "ready"
category = "small-feature"
unit = "stacks"
touches = ["src/stack.rs"]
reference = "src/stack.rs"
new_symbols = true
depends = []

[[tests]]
name = "pop_n_takes_the_top_values_top_first"
file = "tests/pop_n.rs"
asserts = "after push(1), push(2), push(3), pop_n(2) == vec![3, 2] and len() == 1"
+++

## Done

- New method `pub fn pop_n(&mut self, n: usize) -> Vec<T>` on `Stack<T>`, written like `pop`.
- It takes up to `n` values off the top and returns them top first; when `n` is more than `len()` it takes them all, and `pop_n(0)` returns an empty `Vec` and leaves the stack alone.
