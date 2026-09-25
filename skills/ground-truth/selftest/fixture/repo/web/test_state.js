const { colorFor } = require("./state.js");

let failed = 0;

function check(name, fn) {
  try {
    fn();
    console.log("ok - " + name);
  } catch (err) {
    failed += 1;
    console.log("not ok - " + name + ": " + err.message);
  }
}

function assertEqual(actual, expected) {
  if (actual !== expected) {
    throw new Error("expected " + expected + " but got " + actual);
  }
}

check("yes is green", () => assertEqual(colorFor("yes"), "green"));
check("no is red", () => assertEqual(colorFor("no"), "red"));
check("unknown is gray", () => assertEqual(colorFor("unknown"), "gray"));

if (failed > 0) {
  process.exitCode = 1;
}
