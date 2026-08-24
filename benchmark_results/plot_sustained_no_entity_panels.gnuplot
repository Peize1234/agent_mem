set multiplot layout 2,2 rowsfirst title \
    "Sustained Memory Pipeline Load — Entity Extraction Disabled\nLocal LLM/Embedding | SQLite + Embedded Qdrant | Background 1 x 32 | 180 s per point\nGreen: reproduced stable max 2.5 | Amber: queue onset 2.75 | Red: overloaded from 4" \
    font ",18"

set xlabel "Target arrival rate (req/s)"
set ylabel "Average latency (ms, log scale)"
set logscale y 10
set format y "10^{%L}"
set title "A. Retrieval / Add / End-to-End"
plot datafile using 1:5 every ::1 with linespoints ls 1 title "Retrieval avg", \
     datafile using 1:7 every ::1 with linespoints ls 2 title "Add avg", \
     datafile using 1:9 every ::1 with linespoints ls 3 title "E2E avg"

set ylabel "E2E latency (ms, log scale)"
set title "B. E2E Average and P95"
plot datafile using 1:9 every ::1 with linespoints ls 3 title "Average", \
     datafile using 1:10 every ::1 with linespoints ls 5 title "P95"

unset logscale y
set format y "%g"
set ylabel "Rate (req/s)"
set yrange [0:5.5]
set title "C. Offered vs Achieved vs Completed"
plot x with lines dt 2 lw 1.8 lc rgb "#64748b" title "Ideal", \
     datafile using 1:3 every ::1 with linespoints ls 4 title "Achieved arrivals", \
     datafile using 1:4 every ::1 with linespoints ls 1 title "Completion throughput"

set ylabel "Count / seconds (log scale, +1)"
set logscale y 10
set autoscale y
set format y "10^{%L}"
set title "D. Queue and Backlog Signals"
plot datafile using 1:($13+1) every ::1 with linespoints ls 1 title "Peak in-flight + 1", \
     datafile using 1:($17+1) every ::1 with linespoints ls 2 title "Peak background backlog + 1", \
     datafile using 1:($14+1) every ::1 with linespoints ls 3 title "Completion tail seconds + 1"

unset multiplot
