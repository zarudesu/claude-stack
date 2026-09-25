// Maps a logical state to a display colour.
function colorFor(state) {
  if (state === "yes") return "green";
  if (state === "no") return "red";
  return "gray";
}

module.exports = { colorFor };
