import math, random, statistics
SESSION_M=375
def realised(sigma, stressed, n=3000, seed=5):
    random.seed(seed); sm=sigma/math.sqrt(252.0*SESSION_M); out=[]
    for _ in range(n):
        s=23900.0; ref=23900.0; lr=[]
        for m in range(SESSION_M):
            prev=s
            s*=math.exp(-0.5*sm*sm+sm*random.gauss(0,1))
            if stressed and random.random()<0.004:
                s*=math.exp(random.choice([-1,1])*random.uniform(0.0025,0.006))
            lr.append(math.log(s/prev))
        var=sum(x*x for x in lr)
        out.append(math.sqrt(var*252.0))
    return statistics.mean(out)

for sig in (0.105,):
    b=realised(sig,False); s=realised(sig,True)
    print(f"target ann vol {sig:.1%}")
    print(f"  benign   -> realised {b:.2%}")
    print(f"  stressed -> realised {s:.2%}   (jumps add {(s-b)*100:.2f}pp)")
    print()
print("=> the 'stressed' run was quoted as VRP +2pp (IV 12.5 vs 10.5) but true")
print("   realised vol is higher, so the honest VRP there was NEGATIVE.")
print("   Re-run must set IV = realised + intended VRP.")
