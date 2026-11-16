use stacks::stack::Stack;

fn stack_of(values: &[i32]) -> Stack<i32> {
    let mut stack = Stack::new();
    for value in values {
        stack.push(*value);
    }
    stack
}

#[test]
fn zero_takes_nothing() {
    let mut stack = stack_of(&[1, 2]);
    assert!(stack.pop_n(0).is_empty());
    assert_eq!(stack.len(), 2);
}

#[test]
fn more_than_len_takes_all() {
    let mut stack = stack_of(&[1, 2, 3]);
    assert_eq!(stack.pop_n(10), vec![3, 2, 1]);
    assert!(stack.is_empty());
}

#[test]
fn what_is_left_pops_in_order() {
    let mut stack = stack_of(&[1, 2, 3, 4]);
    assert_eq!(stack.pop_n(1), vec![4]);
    assert_eq!(stack.pop(), Some(3));
}
