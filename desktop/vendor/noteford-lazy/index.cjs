'use strict';

// Independently implemented from the consuming API: construct with a factory,
// read .value to evaluate once, and share the result (including a Promise).
// No code from the upstream lazy-val package is incorporated.
class Lazy {
  #factory;
  #ready = false;
  #result;

  constructor(factory) {
    if (typeof factory !== 'function') throw new TypeError('Expected a factory');
    this.#factory = factory;
  }

  get value() {
    if (!this.#ready) {
      const result = this.#factory();
      this.#result = result;
      this.#ready = true;
      this.#factory = undefined;
    }
    return this.#result;
  }

  get hasValue() { return this.#ready; }
}

module.exports = { Lazy };
