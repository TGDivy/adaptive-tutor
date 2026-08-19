At 2,400 requests/second and 2.6 ms, offered CPU service is 6.24 worker-seconds
per second; at 2,800 and 2.8 ms it is 7.84. The latter leaves little capacity
for variance and non-service overhead. A strong answer does not infer a single
cause from utilization. It measures queue residence, runnable delay, service
time, lock wait, and burst shape separately, then ties admission, contention,
or worker-count changes to the evidence.
