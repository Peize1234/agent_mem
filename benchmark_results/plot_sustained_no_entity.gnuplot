datafile = "benchmark_results/sustained_no_entity_180s.csv"

set datafile separator comma
set termoption enhanced
set border linewidth 1.2
set grid ytics xtics lc rgb "#d8dee9" lw 1
set key opaque box width 0.5
set xrange [0.8:5.2]
set xtics ("1" 1, "2" 2, "2.5" 2.5, "2.75" 2.75, "3" 3, "4" 4, "5" 5)
set style line 1 lc rgb "#2563eb" lw 2.6 pt 7 ps 1.0
set style line 2 lc rgb "#f59e0b" lw 2.6 pt 5 ps 1.0
set style line 3 lc rgb "#dc2626" lw 2.8 pt 9 ps 1.0
set style line 4 lc rgb "#059669" lw 2.6 pt 11 ps 1.0
set style line 5 lc rgb "#7c3aed" lw 2.4 pt 13 ps 1.0

set arrow 101 from 2.5, graph 0 to 2.5, graph 1 nohead dt 2 lw 2 lc rgb "#16a34a" front
set arrow 102 from 2.75, graph 0 to 2.75, graph 1 nohead dt 3 lw 1.5 lc rgb "#f59e0b" front
set arrow 103 from 4.0, graph 0 to 4.0, graph 1 nohead dt 2 lw 2 lc rgb "#dc2626" front

set terminal pngcairo size 1800,1200 font "DejaVu Sans,13" enhanced
set output "benchmark_results/sustained_no_entity_180s.png"
load "benchmark_results/plot_sustained_no_entity_panels.gnuplot"

set terminal svg size 1800,1200 font "DejaVu Sans,13" enhanced dynamic
set output "benchmark_results/sustained_no_entity_180s.svg"
load "benchmark_results/plot_sustained_no_entity_panels.gnuplot"
