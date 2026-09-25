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

check("fit is green", () => assertEqual(colorFor("fit"), "green"));
check("unfit is red", () => assertEqual(colorFor("unfit"), "red"));
check("unknown is gray", () => assertEqual(colorFor("unknown"), "gray"));

if (failed > 0) {
  process.exitCode = 1;
}
