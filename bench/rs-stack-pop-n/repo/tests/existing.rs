use stacks::stack::Stack;

#[test]
fn push_then_pop() {
    let mut stack = Stack::new();
    stack.push(7);
    assert_eq!(stack.pop(), Some(7));
    assert!(stack.is_empty());
}
