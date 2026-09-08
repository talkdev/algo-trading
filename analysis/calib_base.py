import math, random, statistics
SESSION_M=375
def realised(sigma, n=2500, seed=5):
    random.seed(seed); sm=sigma/math.sqrt(252.0*SESSION_M); out=[]
    for _ in range(n):
        s=23900.0; v=0.0
        for m in range(SESSION_M):
            prev=s
            s*=math.exp(-0.5*sm*sm+sm*random.gauss(0,1))
            if random.random()<0.004:
                s*=math.exp(random.choice([-1,1])*random.uniform(0.0025,0.006))
            v+=math.log(s/prev)**2
        out.append(math.sqrt(v*252.0))
    return statistics.mean(out)
lo,hi=0.02,0.105
for _ in range(18):
    mid=(lo+hi)/2
    if realised(mid)<0.105: lo=mid
    else: hi=mid
print(f"base diffusion {(lo+hi)/2:.4f} + jumps  ->  realised {realised((lo+hi)/2):.3%}")
