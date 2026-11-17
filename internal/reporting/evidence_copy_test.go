package reporting

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"cheesetrap/internal/domain"
	"cheesetrap/internal/store/db"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

type evidenceCopyDB struct {
	pgx.Tx
	payloads [][]byte
	failAt   int
	failure  error
}

func (d *evidenceCopyDB) Exec(_ context.Context, _ string, args ...any) (pgconn.CommandTag, error) {
	d.payloads = append(d.payloads, bytes.Clone(args[1].([]byte)))
	if len(d.payloads) == d.failAt {
		return pgconn.CommandTag{}, d.failure
	}
	return pgconn.NewCommandTag("INSERT 0 1"), nil
}

func TestEvidenceCopyFailureNeverRetriesOrAcceptsMoreRows(t *testing.T) {
	for _, failAt := range []int{1, 2} {
		sentinel := errors.New("synthetic copy failure")
		database := &evidenceCopyDB{failAt: failAt, failure: sentinel}
		w := evidenceCopyWriter{ctx: context.Background(), q: db.New(database)}
		if err := w.add(make([]domain.ReportEvidence, 2001)); !errors.Is(err, sentinel) {
			t.Fatal("copy failure was lost", err)
		}
		if !errors.Is(w.flush(), sentinel) || !errors.Is(w.add([]domain.ReportEvidence{{}}), sentinel) || len(database.payloads) != failAt {
			t.Fatal("failed copy retried or accepted more work", len(database.payloads))
		}
	}
}

func TestEvidenceCopyCancellationDoesNotFlushPendingRows(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	database := &evidenceCopyDB{}
	w := evidenceCopyWriter{ctx: ctx, q: db.New(database)}
	if err := w.add([]domain.ReportEvidence{{IngestionSeq: 1}}); err != nil {
		t.Fatal(err)
	}
	cancel()
	if !errors.Is(w.flush(), context.Canceled) || !errors.Is(w.add([]domain.ReportEvidence{{IngestionSeq: 2}}), context.Canceled) || len(database.payloads) != 0 {
		t.Fatal("cancelled copy flushed or accepted pending work", len(database.payloads))
	}
}

func TestEvidenceCopiesBoundEncodedBytesWithoutDroppingOversizedRows(t *testing.T) {
	evidence := make([]domain.ReportEvidence, 4)
	for i, size := range []int{700 << 10, 700 << 10, (1 << 20) + 1, 10} {
		evidence[i] = domain.ReportEvidence{IngestionSeq: int64(i + 1), URI: strings.Repeat("x", size)}
	}
	database := &evidenceCopyDB{}
	if err := copyReportEvidence(context.Background(), db.New(database), uuid("70000000-0000-4000-8000-000000000001"), evidence); err != nil {
		t.Fatal(err)
	}
	seen := 0
	for _, payload := range database.payloads {
		var rows []json.RawMessage
		if err := json.Unmarshal(payload, &rows); err != nil {
			t.Fatal(err)
		}
		if len(payload) > 1<<20 && len(rows) != 1 {
			t.Fatalf("copy statement has %d encoded bytes and %d rows; oversized rows must stand alone", len(payload), len(rows))
		}
		for _, row := range rows {
			if seen >= len(evidence) {
				t.Fatal("duplicated evidence")
			}
			want, err := json.Marshal(evidence[seen])
			if err != nil || !bytes.Equal(row, want) {
				t.Fatal("copied evidence bytes or order changed", seen, err)
			}
			seen++
		}
	}
	if seen != len(evidence) {
		t.Fatal("copy tail was lost", seen)
	}
}
