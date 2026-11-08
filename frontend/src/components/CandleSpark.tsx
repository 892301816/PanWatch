/** 选股列表的迷你 K 线:蜡烛 + 量能。宽度由容器给定,约 2:1 才看得出起伏。 */

export interface SparkBar {
  o: number
  h: number
  l: number
  c: number
  v?: number
}

/** 近 20 日迷你K线(蜡烛 + 量能)。宽高由容器给定,约 2:1 才看得出起伏 */
export function CandleSpark({ bars, pending }: { bars?: SparkBar[]; pending: boolean }) {
  if (!bars || bars.length < 2) {
    return (
      <div className="flex h-full w-full items-center justify-center text-[10px] text-muted-foreground/70">
        {pending ? 'K 线加载中…' : '暂无K线'}
      </div>
    )
  }
  const w = 160
  const h = 96
  const volH = 24 // 底部量能区高度
  const gap = 5
  const priceH = h - volH - gap
  const hi = Math.max(...bars.map((b) => b.h))
  const lo = Math.min(...bars.map((b) => b.l))
  const span = hi - lo || 1
  const bw = w / bars.length
  const cw = Math.max(2, bw * 0.8)
  const y = (v: number) => 3 + ((hi - v) / span) * (priceH - 6)
  const volTop = priceH + gap
  const maxVol = Math.max(...bars.map((b) => b.v || 0)) || 1
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-full w-full" preserveAspectRatio="none">
      {bars.map((b, i) => {
        const x = i * bw + bw / 2
        const rise = b.c >= b.o
        const color = rise ? '#F04438' : '#12B76A'
        const top = y(Math.max(b.o, b.c))
        const bodyH = Math.max(2, Math.abs(y(b.o) - y(b.c)))
        const vh = Math.max(1, ((b.v || 0) / maxVol) * (volH - 1))
        return (
          <g key={i}>
            <line x1={x} y1={y(b.h)} x2={x} y2={y(b.l)} stroke={color} strokeWidth="1" />
            <rect x={x - cw / 2} y={top} width={cw} height={bodyH} fill={color} />
            <rect
              x={x - cw / 2}
              y={volTop + (volH - vh)}
              width={cw}
              height={vh}
              fill={color}
              opacity="0.45"
            />
          </g>
        )
      })}
    </svg>
  )
}
