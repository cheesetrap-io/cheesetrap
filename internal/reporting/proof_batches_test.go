package reporting

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"cheesetrap/internal/domain"
	"cheesetrap/internal/store/db"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type proofBatchDB struct {
	calls      [][]any
	failAt     int
	sealed     bool
	shortWrite bool
}

func (d *proofBatchDB) Exec(ctx context.Context, _ string, args ...any) (pgconn.CommandTag, error) {
	if err := ctx.Err(); err != nil {
		return pgconn.CommandTag{}, err
	}
	d.calls = append(d.calls, args)
	if len(d.calls) == d.failAt {
		return pgconn.CommandTag{}, errors.New("proof write failed")
	}
	count := 1
	if parts, ok := args[5].([][]byte); ok {
		count = len(parts)
	}
	if d.shortWrite {
		count--
	}
	return pgconn.NewCommandTag(fmt.Sprintf("INSERT 0 %d", count)), nil
}

func TestProofWriteByteBudgetAndSingleOversizedPart(t *testing.T) {
	database := &proofBatchDB{}
	w := &proofWriter{ctx: context.Background(), q: db.New(database), report: "report",
		offsets: map[string]int{}, ordinals: map[string]int{}}
	for _, size := range []int{700 << 10, 700 << 10, (1 << 20) + 1, 10} {
		if err := w.part("rules", []map[string]string{{"data": strings.Repeat("x", size)}}, 1); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.seal(&domain.ReportSnapshot{}); err != nil {
		t.Fatal(err)
	}
	seen := 0
	for _, call := range database.calls {
		payloads := call[5].([][]byte)
		bytes := 0
		for _, payload := range payloads {
			bytes += len(payload)
			seen++
		}
		if bytes > 1<<20 && len(payloads) != 1 {
			t.Fatal("oversized parts must be isolated")
		}
	}
	if seen != 4 {
		t.Fatal("byte budget dropped proof parts", seen)
	}
}

func TestProofWritesPreserveIndependentKindOffsets(t *testing.T) {
	database := &proofBatchDB{}
	w := &proofWriter{ctx: context.Background(), q: db.New(database), report: "report",
		offsets: map[string]int{}, ordinals: map[string]int{}}
	for _, part := range []struct {
		kind  string
		count int
	}{{"rules", 1}, {"evidence", 2}, {"rules", 1}, {"evidence", 1}} {
		values := make([]map[string]int, part.count)
		for i := range values {
			values[i] = map[string]int{"id": i}
		}
		if err := w.part(part.kind, values, part.count); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.seal(&domain.ReportSnapshot{}); err != nil {
		t.Fatal(err)
	}
	call := database.calls[0]
	if fmt.Sprint(call[1]) != "[rules evidence rules evidence]" || fmt.Sprint(call[2]) != "[0 0 1 1]" || fmt.Sprint(call[3]) != "[0 0 1 2]" || fmt.Sprint(call[4]) != "[1 2 1 1]" {
		t.Fatal("proof kinds lost their independent order or offsets", call[:5])
	}
	if err := w.part("rules", []map[string]int{{"id": 99}}, 1); err == nil {
		t.Fatal("sealed proof accepted another part")
	}
}

func TestProofWriteFailureNeverSealsOrRetries(t *testing.T) {
	for _, count := range []int{32, 35, 65} {
		for _, failAt := range []int{1, 2} {
			if count == 32 && failAt == 2 {
				continue
			}
			t.Run(fmt.Sprintf("parts-%d-fail-%d", count, failAt), func(t *testing.T) {
				database := &proofBatchDB{failAt: failAt}
				w := &proofWriter{ctx: context.Background(), q: db.New(database), report: "report", offsets: map[string]int{}, ordinals: map[string]int{}}
				for i := 0; i < count; i++ {
					if w.part("rules", []map[string]int{{"id": i}}, 1) != nil {
						break
					}
				}
				var snapshot domain.ReportSnapshot
				if err := w.seal(&snapshot); err == nil {
					t.Fatal("failed write accepted")
				}
				if database.sealed || snapshot.Proof != nil || len(database.calls) != failAt {
					t.Fatal("failed batch reached the seal query")
				}
				if err := w.part("rules", []map[string]int{{"id": 99}}, 1); err == nil {
					t.Fatal("writer accepted work after failure")
				}
				if len(database.calls) != failAt {
					t.Fatal("failed write retried")
				}
			})
		}
	}
}

func TestProofShortWriteAndCancellationCannotSeal(t *testing.T) {
	for _, mode := range []string{"short-write", "cancelled"} {
		t.Run(mode, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			database := &proofBatchDB{shortWrite: mode == "short-write"}
			w := &proofWriter{ctx: ctx, q: db.New(database), report: "report", offsets: map[string]int{}, ordinals: map[string]int{}}
			if err := w.part("rules", []map[string]int{{"id": 1}}, 1); err != nil {
				t.Fatal(err)
			}
			if mode == "cancelled" {
				cancel()
			}
			err := w.seal(&domain.ReportSnapshot{})
			if err == nil || database.sealed {
				t.Fatal("incomplete write reached seal")
			}
			if mode == "cancelled" && !errors.Is(err, context.Canceled) {
				t.Fatal("lost cancellation", err)
			}
		})
	}
}
func (*proofBatchDB) Query(context.Context, string, ...any) (pgx.Rows, error) {
	panic("unexpected proof query")
}
func (d *proofBatchDB) QueryRow(context.Context, string, ...any) pgx.Row {
	d.sealed = true
	return proofBatchRow{}
}

type proofBatchRow struct{}

func (proofBatchRow) Scan(values ...any) error {
	*values[0].(*[]byte) = []byte(`{"scheme":"sha256-parts-v1","evidence_count":1,"rule_count":65}`)
	return nil
}

func TestProofWritesKeepExactPartsAcrossBoundedStatements(t *testing.T) {
	database := &proofBatchDB{}
	w := &proofWriter{ctx: context.Background(), q: db.New(database), report: "report",
		offsets: map[string]int{}, ordinals: map[string]int{}}
	for i := 0; i < 65; i++ {
		if err := w.part("rules", []map[string]int{{"id": i}}, 1); err != nil {
			t.Fatal(err)
		}
	}
	var snapshot domain.ReportSnapshot
	if err := w.seal(&snapshot); err != nil {
		t.Fatal(err)
	}
	if len(database.calls) != 3 {
		t.Fatalf("write statements=%d, want 3 bounded statements", len(database.calls))
	}
	seen := 0
	for _, call := range database.calls {
		kinds, ordinals, offsets, counts, payloads := call[1].([]string), call[2].([]int64), call[3].([]int64), call[4].([]int32), call[5].([][]byte)
		if len(payloads) == 0 || len(payloads) > 32 {
			t.Fatal("unbounded proof statement", len(payloads))
		}
		for n, payload := range payloads {
			if call[0] != "report" || kinds[n] != "rules" || ordinals[n] != int64(seen) || offsets[n] != int64(seen) || counts[n] != 1 || !bytes.Equal(payload, []byte(fmt.Sprintf(`[{"id":%d}]`, seen))) {
				t.Fatal("signed proof part bytes, order, count or ownership changed", seen)
			}
			seen++
		}
	}
	if seen != 65 || !database.sealed {
		t.Fatal("proof tail was not written before sealing", seen)
	}
}
