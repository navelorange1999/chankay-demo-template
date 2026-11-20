const angle = document.querySelector("#angle");
const degrees = document.querySelector("#degrees");
const tile = document.querySelector(".tile");
angle.addEventListener("input", () => {
  const value = angle.valueAsNumber;
  if (Number.isFinite(value) && value >= -180 && value <= 180) {
    degrees.value = value + "°";
    tile.style.rotate = value + "deg";
  }
});
