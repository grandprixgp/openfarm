use ringbuf::ring::Ring;

#[test]
fn a_new_ring_is_empty() {
    assert!(Ring::new(3).items().is_empty());
}
