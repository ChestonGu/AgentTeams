// bench_s3.go — S3 层瓶颈复现基准 v2（复用真实桶，不预置负载）
//
// 设计: 不向桶里压入测试对象（省 seed 成本），直接复用场景中已有的 S3 桶:
//   - 读空间: 从 -prefix 下采样真实 key 池，做 stat/get/list（只读，零污染）
//   - 写空间: bench-probe/ 前缀，做 put（-write=false 关闭），跑完 -clean 清理
//   - 操作类型对比（主目标）: 在环境背景调谐压力下，对比 stat/get/put/list
//     各类型的单次调用延迟分布，直接暴露"哪种类型负载最耗时"（E7 主实验）
//   - 调用模式对比: mc vs sdk 同负载对比，量化 exec/TLS/无连接池开销（E1 辅助）
//   - 背景规模: 启动时全桶统计对象数记入 CSV（background 列）；云 S3 全桶 LIST
//     分页开销大且耗 QPS，默认关闭（-count=false），用 OSS 控制台对象数代替；
//     用同一命令换 -bucket/-prefix 指向不同规模桶即规模效应实验（E2）
//   - 测试量小: 环境里其他 team 的 worker 每 5min 周期调谐/事件调谐本身就在
//     持续打 S3 负载，bench 只做小样本"搭车测量"，无需大 rounds/workers
//
// 用法:
//
//	go mod init bench-s3 && go get github.com/minio/minio-go/v7
//	# 对场景桶跑完整负载（mc vs sdk，含写路径，跑完自动清 bench-probe/）:
//	go run bench_s3.go -endpoint http://127.0.0.1:9000 -ak minioadmin -sk minioadmin \
//	  -bucket hiclaw -prefix agents/ -mc /usr/local/bin/mc -alias bench \
//	  -drivers mc,sdk -workers 1,5 -rounds 10
//	# 只读探测（不改动桶任何数据）:
//	... 同参数 -write=false
//	# 规模对比: 同一命令换 -bucket/-prefix 指向不同规模桶，对比 CSV 的 background 列
//	# 背景负载强度对比: 同一桶在调谐高峰/低谷时段各跑一次（比加大 rounds 更有意义）
//	# 冷启动数据: -warmup 0
//
// 输出 CSV 到 stdout: driver,background,workers,op,count,avg_ms,p50_ms,p95_ms,p99_ms
// op=round 行 = 每成员轮次（默认 12 GET + 3 PUT + 2 STAT + 1 LIST）墙钟耗时，
// 直接对标真实 config 阶段。
//
// 注意: 对生产桶跑 bench 会向生产存储打真实负载，建议低 rounds 或非高峰时段。
package main

import (
	"bytes"
	"context"
	"encoding/csv"
	"flag"
	"fmt"
	"io"
	"math/rand"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// 默认轮次配比: 对齐 DeployWorkerConfig 每成员每轮的真实操作配比（GET 主导）
const (
	poolSize   = 64 // 真实 key 采样池上限
	probeWrite = 16 // 写空间 key 数（bench-probe/ 前缀）
)

var (
	flagEndpoint = flag.String("endpoint", "http://127.0.0.1:9000", "S3 endpoint（scheme 决定 TLS，换 endpoint 即换引擎/网络实验）")
	flagAK       = flag.String("ak", "minioadmin", "access key")
	flagSK       = flag.String("sk", "minioadmin", "secret key")
	flagBucket   = flag.String("bucket", "hiclaw", "被测桶（场景真实桶，读空间不修改任何数据）")
	flagPrefix   = flag.String("prefix", "agents/", "被测真实 key 前缀（读空间采样范围）")
	flagOps      = flag.String("ops", "stat,get,put,list", "操作类型集合: stat,get,put,list")
	flagWrite    = flag.Bool("write", true, "写路径测试（put 写到 bench-probe/ 前缀，跑完清理）")
	flagWriteSz  = flag.Int("write-size", 8192, "写空间对象大小字节（E3 对象大小实验用）")
	flagMC       = flag.String("mc", "mc", "mc 二进制路径")
	flagAlias    = flag.String("alias", "bench", "mc alias 名")
	flagDrivers  = flag.String("drivers", "mc,sdk", "驱动（逗号分隔）: mc=子进程现状, sdk=minio-go 连接池")
	flagWorkers  = flag.String("workers", "1,5", "并发 worker 数（逗号分隔）；小样本即可，环境自身调谐就是负载")
	flagRounds   = flag.Int("rounds", 10, "每 worker 正式计时的轮次数（小样本，背景负载由其他 team 调谐提供）")
	flagWarmup   = flag.Int("warmup", 2, "正式计时前的预热轮次（抹平服务端缓存顺序偏差）")
	flagClean    = flag.Bool("clean", true, "run 结束时清理写空间 bench-probe/（防残留污染）")
	flagProbeN   = flag.Int("probe-n", 64, "真实 key 采样池大小")
	flagCount    = flag.Bool("count", false, "全桶统计对象数（云 S3 上 LIST 分页贵，默认关，用控制台对象数）")
)

type rec struct {
	driver     string
	background int // 桶内真实对象总数（CSV background 列）
	workers    int
	op         string // get/put/stat/list/round
	ms         []float64
}

func main() {
	flag.Parse()
	ws := parseInts(*flagWorkers)
	drvs := strings.Split(*flagDrivers, ",")
	ops := parseOps(*flagOps)

	ctx := context.Background()
	fmt.Fprintf(os.Stderr, "connecting: endpoint=%s bucket=%s ...\n", *flagEndpoint, *flagBucket)
	sdk := newSDK()
	ensureBucket(ctx, sdk)
	setupMC() // mc alias set（仅一次，对齐生产静态凭据模式）

	// 背景规模: 默认不统计（云 S3 全桶 LIST 分页贵且耗 QPS），-count 开启；
	// 推荐直接用 OSS 控制台的对象数
	background := -1
	if *flagCount {
		background = countObjects(ctx, sdk)
	}
	fmt.Fprintf(os.Stderr, "bucket %q background=%d (-count 开启全桶统计；云 S3 建议用控制台对象数)\n", *flagBucket, background)

	// 读空间: 从真实前缀采样 key 池（不修改任何数据）
	fmt.Fprintf(os.Stderr, "sampling read keys: prefix=%q probe=%d ...\n", *flagPrefix, *flagProbeN)
	t0 := time.Now()
	readKeys := sampleKeys(ctx, sdk, *flagPrefix, *flagProbeN)
	fmt.Fprintf(os.Stderr, "sampled %d read keys (%s)\n", len(readKeys), time.Since(t0))
	if len(readKeys) == 0 {
		fmt.Fprintf(os.Stderr, "fatal: prefix %q 下没有对象\n", *flagPrefix)
		os.Exit(1)
	}

	// 写空间 key（bench-probe/ 前缀，跑完清理）
	writeKeys := make([]string, probeWrite)
	for i := range writeKeys {
		writeKeys[i] = fmt.Sprintf("bench-probe/%03d", i)
	}
	writeBlob := make([]byte, *flagWriteSz)
	rand.New(rand.NewSource(42)).Read(writeBlob)

	tmpDir, err := os.MkdirTemp("", "bench-s3-")
	must(err)
	defer os.RemoveAll(tmpDir)

	var all []*rec
	for _, w := range ws {
		for _, d := range drvs {
			for _, r := range benchOnce(ctx, d, sdk, w, ops, readKeys, writeKeys, writeBlob, tmpDir) {
				r.background = background
				r.workers = w
				all = append(all, r)
			}
		}
	}
	writeCSV(all)
	writeSummary(all) // 人眼可读的操作类型对比（主目标输出）

	if *flagClean {
		// 只清写空间，不动真实数据
		cleanPrefix(ctx, sdk, "bench-probe/")
	}
}

// parseOps 把 "stat,get,put,list" 解析为集合；put 受 -write 开关控制
func parseOps(s string) map[string]bool {
	m := map[string]bool{}
	for _, o := range strings.Split(s, ",") {
		o = strings.TrimSpace(o)
		if o != "" && (o == "stat" || o == "get" || o == "put" || o == "list") {
			m[o] = true
		}
	}
	if !*flagWrite {
		delete(m, "put")
	}
	return m
}

// benchOnce 以 W 个并发 worker 各跑 rounds 轮，返回各 op 与 round 的耗时样本
func benchOnce(ctx context.Context, drv string, sdk *minio.Client, workers int, ops map[string]bool, readKeys, writeKeys []string, blob []byte, tmpDir string) []*rec {
	// 每轮配比（对齐生产 config 阶段: GET 主导，12 GET + 3 PUT + 2 STAT + 1 LIST）
	var mix []string
	if ops["get"] {
		for i := 0; i < 12; i++ {
			mix = append(mix, "get")
		}
	}
	if ops["put"] {
		for i := 0; i < 3; i++ {
			mix = append(mix, "put")
		}
	}
	if ops["stat"] {
		for i := 0; i < 2; i++ {
			mix = append(mix, "stat")
		}
	}
	if ops["list"] {
		mix = append(mix, "list")
	}

	var mu sync.Mutex
	times := map[string][]float64{}
	var roundMs []float64

	// 进度输出: 长跑（尤其 mc 驱动 + 高 S3 延迟）期间保持可见，避免"假死"观感。
	// CSV 仍只走 stdout，进度全部走 stderr，不影响结果解析。
	total := int64(workers * *flagRounds)
	var progress atomic.Int64
	fmt.Fprintf(os.Stderr, "== bench: driver=%s workers=%d (warmup=%d rounds=%d) ==\n", drv, workers, *flagWarmup, *flagRounds)
	comboStart := time.Now()

	var wg sync.WaitGroup
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			rng := rand.New(rand.NewSource(int64(w)))
			tmp := filepath.Join(tmpDir, fmt.Sprintf("put-%d.tmp", w))
			for r := 0; r < *flagWarmup+*flagRounds; r++ {
				rstart := time.Now()
				for _, op := range mix {
					start := time.Now()
					var err error
					switch op {
					case "get":
						err = opGet(ctx, drv, sdk, readKeys[rng.Intn(len(readKeys))])
					case "put":
						err = opPut(ctx, drv, sdk, writeKeys[rng.Intn(len(writeKeys))], blob, tmp)
					case "stat":
						err = opStat(ctx, drv, sdk, readKeys[rng.Intn(len(readKeys))])
					case "list":
						err = opList(ctx, drv, sdk)
					}
					if err != nil {
						fmt.Fprintf(os.Stderr, "fatal: %s %s: %v\n", drv, op, err)
						os.Exit(1)
					}
					elapsed := time.Since(start).Seconds() * 1000
					if r < *flagWarmup {
						continue // 预热轮: 不记录，让服务端缓存进入热态
					}
					mu.Lock()
					times[op] = append(times[op], elapsed)
					mu.Unlock()
				}
				if r < *flagWarmup {
					continue
				}
				mu.Lock()
				roundMs = append(roundMs, time.Since(rstart).Seconds()*1000)
				mu.Unlock()
				// 每完成一轮打一次进度（多 worker 合计数），\r 原地刷新
				if n := progress.Add(1); n != total && (n == 1 || n%10 == 0) {
					fmt.Fprintf(os.Stderr, "\r  driver=%s workers=%d rounds %d/%d  ", drv, workers, n, total)
				}
			}
		}(w)
	}
	wg.Wait()
	fmt.Fprintf(os.Stderr, "== bench: driver=%s workers=%d done (%d rounds in %s) ==\n", drv, workers, progress.Load(), time.Since(comboStart))

	var out []*rec
	for _, op := range []string{"get", "put", "stat", "list"} {
		if len(times[op]) > 0 {
			out = append(out, &rec{driver: drv, op: op, ms: times[op]})
		}
	}
	out = append(out, &rec{driver: drv, op: "round", ms: roundMs})
	return out
}

// ---- 两个驱动：mc 子进程（现状）与 minio-go SDK（优化方案）----

func mcRun(ctx context.Context, args ...string) error {
	cmd := exec.CommandContext(ctx, *flagMC, args...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("mc %s: %w (%s)", strings.Join(args, " "), err, strings.TrimSpace(stderr.String()))
	}
	return nil
}

func opGet(ctx context.Context, drv string, sdk *minio.Client, key string) error {
	switch drv {
	case "mc":
		return mcRun(ctx, "cat", *flagAlias+"/"+*flagBucket+"/"+key)
	case "sdk":
		obj, err := sdk.GetObject(ctx, *flagBucket, key, minio.GetObjectOptions{})
		if err != nil {
			return err
		}
		defer obj.Close()
		_, err = io.Copy(io.Discard, obj)
		return err
	}
	return fmt.Errorf("unknown driver %q", drv)
}

func opPut(ctx context.Context, drv string, sdk *minio.Client, key string, blob []byte, tmp string) error {
	switch drv {
	case "mc":
		// 镜像 minio.go PutObject: 先写临时文件再 mc cp
		if err := os.WriteFile(tmp, blob, 0o644); err != nil {
			return err
		}
		return mcRun(ctx, "cp", tmp, *flagAlias+"/"+*flagBucket+"/"+key)
	case "sdk":
		_, err := sdk.PutObject(ctx, *flagBucket, key, bytes.NewReader(blob), int64(len(blob)), minio.PutObjectOptions{})
		return err
	}
	return fmt.Errorf("unknown driver %q", drv)
}

func opStat(ctx context.Context, drv string, sdk *minio.Client, key string) error {
	switch drv {
	case "mc":
		return mcRun(ctx, "stat", *flagAlias+"/"+*flagBucket+"/"+key)
	case "sdk":
		_, err := sdk.StatObject(ctx, *flagBucket, key, minio.StatObjectOptions{})
		return err
	}
	return fmt.Errorf("unknown driver %q", drv)
}

func opList(ctx context.Context, drv string, sdk *minio.Client) error {
	switch drv {
	case "mc":
		return mcRun(ctx, "ls", *flagAlias+"/"+*flagBucket+"/"+*flagPrefix)
	case "sdk":
		// 取首响应即返回: LIST 延迟 = 服务端返回首个条目耗时（非全量遍历）
		for obj := range sdk.ListObjects(ctx, *flagBucket, minio.ListObjectsOptions{Prefix: *flagPrefix}) {
			if obj.Err != nil {
				return obj.Err
			}
			break
		}
		return nil
	}
	return fmt.Errorf("unknown driver %q", drv)
}

// ---- 初始化与工具 ----

func newSDK() *minio.Client {
	secure := strings.HasPrefix(*flagEndpoint, "https://")
	host := strings.TrimPrefix(*flagEndpoint, "https://")
	host = strings.TrimPrefix(host, "http://")
	c, err := minio.New(host, &minio.Options{
		Creds:  credentials.NewStaticV4(*flagAK, *flagSK, ""),
		Secure: secure,
	})
	must(err)
	return c
}

func ensureBucket(ctx context.Context, sdk *minio.Client) {
	exists, err := sdk.BucketExists(ctx, *flagBucket)
	must(err)
	if !exists {
		fmt.Fprintf(os.Stderr, "fatal: bucket %q 不存在（bench 不创建桶，只复用场景已有桶）\n", *flagBucket)
		os.Exit(1)
	}
}

func setupMC() {
	if err := mcRun(context.Background(), "alias", "set", *flagAlias, *flagEndpoint, *flagAK, *flagSK); err != nil {
		fmt.Fprintln(os.Stderr, "fatal:", err)
		os.Exit(1)
	}
}

// countObjects 全桶统计对象数（一次 LIST 递归遍历，服务端负担远小于预置负载）
func countObjects(ctx context.Context, sdk *minio.Client) int {
	n := 0
	for obj := range sdk.ListObjects(ctx, *flagBucket, minio.ListObjectsOptions{Recursive: true}) {
		if obj.Err != nil {
			fmt.Fprintf(os.Stderr, "list warning: %v\n", obj.Err)
			break
		}
		n++
	}
	return n
}

// sampleKeys 从真实前缀采样至多 n 个 key 作为读空间（只读，不修改任何数据）
func sampleKeys(ctx context.Context, sdk *minio.Client, prefix string, n int) []string {
	var keys []string
	for obj := range sdk.ListObjects(ctx, *flagBucket, minio.ListObjectsOptions{Prefix: prefix, Recursive: true}) {
		if obj.Err != nil {
			fmt.Fprintf(os.Stderr, "list warning: %v\n", obj.Err)
			break
		}
		keys = append(keys, obj.Key)
		if len(keys) >= n {
			break
		}
	}
	return keys
}

// cleanPrefix 删除指定前缀下全部对象（prefix="" 即清空整个 bucket）。
// 用 SDK 批量删除（LIST + 批量 DELETE），不用 mc，避免 bench 依赖清理路径。
func cleanPrefix(ctx context.Context, sdk *minio.Client, prefix string) {
	objectsCh := sdk.ListObjects(ctx, *flagBucket, minio.ListObjectsOptions{Prefix: prefix, Recursive: true})
	removeCh := sdk.RemoveObjects(ctx, *flagBucket, objectsCh, minio.RemoveObjectsOptions{})
	for e := range removeCh {
		if e.Err != nil {
			fmt.Fprintf(os.Stderr, "cleanup warning: %v\n", e.Err)
		}
	}
}

func writeCSV(recs []*rec) {
	w := csv.NewWriter(os.Stdout)
	_ = w.Write([]string{"driver", "background", "workers", "op", "count", "avg_ms", "p50_ms", "p95_ms", "p99_ms"})
	for _, r := range recs {
		if len(r.ms) == 0 {
			continue
		}
		sorted := append([]float64(nil), r.ms...)
		sort.Float64s(sorted)
		var sum float64
		for _, v := range sorted {
			sum += v
		}
		_ = w.Write([]string{
			r.driver, strconv.Itoa(r.background), strconv.Itoa(r.workers), r.op,
			strconv.Itoa(len(sorted)),
			strconv.FormatFloat(sum/float64(len(sorted)), 'f', 1, 64),
			strconv.FormatFloat(pct(sorted, 0.50), 'f', 1, 64),
			strconv.FormatFloat(pct(sorted, 0.95), 'f', 1, 64),
			strconv.FormatFloat(pct(sorted, 0.99), 'f', 1, 64),
		})
	}
	w.Flush()
}

// writeSummary 输出人眼可读的操作类型对比（主目标: 背景压力下哪种负载最耗时）
func writeSummary(recs []*rec) {
	type agg struct {
		avg, p50, p95 float64
	}
	by := map[string]map[string]agg{} // driver -> op -> agg
	for _, r := range recs {
		if len(r.ms) == 0 {
			continue
		}
		sorted := append([]float64(nil), r.ms...)
		sort.Float64s(sorted)
		var sum float64
		for _, v := range sorted {
			sum += v
		}
		if by[r.driver] == nil {
			by[r.driver] = map[string]agg{}
		}
		by[r.driver][r.op] = agg{sum / float64(len(sorted)), pct(sorted, 0.50), pct(sorted, 0.95)}
	}
	fmt.Fprintln(os.Stderr, "\n== 操作类型对比（背景压力下单次调用延迟 ms；round=一成员轮次墙钟）==")
	for d, ops := range by {
		fmt.Fprintf(os.Stderr, "driver=%s\n", d)
		fmt.Fprintf(os.Stderr, "  %-6s %9s %9s %9s\n", "op", "avg", "p50", "p95")
		for _, op := range []string{"stat", "get", "put", "list", "round"} {
			if a, ok := ops[op]; ok {
				fmt.Fprintf(os.Stderr, "  %-6s %9.1f %9.1f %9.1f\n", op, a.avg, a.p50, a.p95)
			}
		}
	}
}

func pct(sorted []float64, p float64) float64 {
	if len(sorted) == 0 {
		return 0
	}
	idx := int(p * float64(len(sorted)))
	if idx >= len(sorted) {
		idx = len(sorted) - 1
	}
	return sorted[idx]
}

func parseInts(s string) []int {
	var out []int
	for _, p := range strings.Split(s, ",") {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}
		v, err := strconv.Atoi(p)
		must(err)
		out = append(out, v)
	}
	return out
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "fatal:", err)
		os.Exit(1)
	}
}
