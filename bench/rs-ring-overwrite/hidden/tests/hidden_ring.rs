use ringbuf::ring::Ring;

#[test]
fn below_capacity_keeps_everything() {
    let mut ring = Ring::new(3);
    ring.push(1);
    ring.push(2);
    assert_eq!(ring.items(), &[1, 2]);
}

#[test]
fn many_pushes_keep_the_last_in_order() {
    let mut ring = Ring::new(3);
    for value in 1..=10 {
        ring.push(value);
    }
    assert_eq!(ring.items(), &[8, 9, 10]);
}

#[test]
fn a_zero_ring_keeps_nothing() {
    let mut ring = Ring::new(0);
    ring.push(1);
    ring.push(2);
    assert!(ring.items().is_empty());
}
