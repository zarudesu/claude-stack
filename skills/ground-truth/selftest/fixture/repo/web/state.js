// Maps a logical state to a display colour.
function colorFor(state) {
  if (state === "fit") return "green";
  if (state === "unfit") return "red";
  return "gray";
}

module.exports = { colorFor };
