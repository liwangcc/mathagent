// Grading stopwatch display: 42 -> "0:42", 125 -> "2:05", 3725 -> "1:02:05".
export function formatGradingDuration(seconds) {
  const total = seconds == null ? Number.NaN : Math.floor(Number(seconds))
  if (!Number.isFinite(total) || total < 0) return '--:--'
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const rest = String(total % 60).padStart(2, '0')
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${rest}` : `${minutes}:${rest}`
}
