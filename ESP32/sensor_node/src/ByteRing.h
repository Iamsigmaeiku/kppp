#pragma once

#include <stddef.h>
#include <stdint.h>

/** Byte ring buffer, drop-oldest on overflow. ISR/task safe with external mux. */
class ByteRing {
 public:
  explicit ByteRing(uint8_t *storage, size_t cap)
      : buf_(storage), cap_(cap), head_(0), tail_(0), size_(0) {}

  size_t capacity() const { return cap_; }
  size_t size() const { return size_; }
  size_t free() const { return cap_ - size_; }
  bool empty() const { return size_ == 0; }

  size_t write(const uint8_t *data, size_t n) {
    if (!data || n == 0 || !buf_ || cap_ == 0) return 0;
    for (size_t i = 0; i < n; i++) {
      if (size_ >= cap_) {
        // drop oldest byte
        head_ = (head_ + 1) % cap_;
        size_--;
      }
      buf_[tail_] = data[i];
      tail_ = (tail_ + 1) % cap_;
      size_++;
    }
    return n;
  }

  size_t read(uint8_t *out, size_t n) {
    if (!out || n == 0 || size_ == 0) return 0;
    const size_t take = n < size_ ? n : size_;
    for (size_t i = 0; i < take; i++) {
      out[i] = buf_[head_];
      head_ = (head_ + 1) % cap_;
    }
    size_ -= take;
    return take;
  }

  void clear() {
    head_ = tail_ = size_ = 0;
  }

  int peek(size_t i) const {
    if (i >= size_) return -1;
    return buf_[(head_ + i) % cap_];
  }

 private:
  uint8_t *buf_;
  size_t cap_;
  size_t head_;
  size_t tail_;
  size_t size_;
};
