// Pages whose route-leave guard would interrupt in-flight work register here,
// so automated navigation (the usage tour) can stay put instead of triggering
// the "leave and abort?" confirm on the teacher's behalf.
const busyPages = new Set()

export function setRouteBusy(key, busy) {
  if (busy) busyPages.add(key)
  else busyPages.delete(key)
}

export function isRouteBusy() {
  return busyPages.size > 0
}
