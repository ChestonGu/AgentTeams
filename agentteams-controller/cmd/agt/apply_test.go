package main

import (
	"archive/zip"
	"bytes"
	"fmt"
	"strings"
	"testing"
)

func buildZip(t *testing.T, files map[string]string) []byte {
	t.Helper()
	buf := &bytes.Buffer{}
	w := zip.NewWriter(buf)
	for name, content := range files {
		f, err := w.Create(name)
		if err != nil {
			t.Fatalf("create %s: %v", name, err)
		}
		if _, err := f.Write([]byte(content)); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatalf("close zip: %v", err)
	}
	return buf.Bytes()
}

func TestExtractWorkerFieldsFromZip(t *testing.T) {
	cases := []struct {
		name          string
		manifest      string
		wantModel     string
		wantRuntime   string
		wantAdapter   string
		wantParams    map[string]string
	}{
		{
			name:        "empty zip, no manifest",
			manifest:    "",
			wantModel:   "",
			wantRuntime: "",
		},
		{
			name:        "manifest without worker block uses top-level fields",
			manifest:    `{"model":"top","runtime":"copaw"}`,
			wantModel:   "top",
			wantRuntime: "copaw",
		},
		{
			name:        "worker block overrides top-level (matches doc schema)",
			manifest:    `{"model":"top","runtime":"openclaw","worker":{"model":"nested","runtime":"copaw"}}`,
			wantModel:   "nested",
			wantRuntime: "copaw",
		},
		{
			name:        "worker block partially overrides leaves other top-level intact",
			manifest:    `{"runtime":"openclaw","worker":{"model":"only-model"}}`,
			wantModel:   "only-model",
			wantRuntime: "openclaw",
		},
		{
			name:        "adapterMode flows from worker block",
			manifest:    `{"runtime":"worker-bridge","worker":{"adapterMode":"cimicode-stateless"}}`,
			wantModel:   "",
			wantRuntime: "worker-bridge",
			wantAdapter: "cimicode-stateless",
		},
		{
			name:        "runtimeParameter flows from top level",
			manifest:    `{"runtime":"worker-bridge","runtimeParameter":{"baseUrl":"https://gw.example.com","region":"cn-north-7"}}`,
			wantModel:   "",
			wantRuntime: "worker-bridge",
			wantParams:  map[string]string{"baseUrl": "https://gw.example.com", "region": "cn-north-7"},
		},
		{
			name:        "worker block runtimeParameter overrides top level",
			manifest:    `{"runtime":"worker-bridge","runtimeParameter":{"baseUrl":"top"},"worker":{"adapterMode":"cimicode-stateless","runtimeParameter":{"sessionId":"sess-1","sandboxId":"sbx-1"}}}`,
			wantModel:   "",
			wantRuntime: "worker-bridge",
			wantAdapter: "cimicode-stateless",
			wantParams:  map[string]string{"sessionId": "sess-1", "sandboxId": "sbx-1"},
		},
		{
			name:        "non-string or empty runtimeParameter values are dropped",
			manifest:    `{"runtimeParameter":{"sessionId":"keep","sandboxId":"","count":3}}`,
			wantModel:   "",
			wantRuntime: "",
			wantParams:  map[string]string{"sessionId": "keep"},
		},
		{
			name:        "missing fields stay empty so caller defaults can apply",
			manifest:    `{"worker":{"suggested_name":"alice"}}`,
			wantModel:   "",
			wantRuntime: "",
		},
		{
			name:        "invalid JSON returns empty (caller falls back)",
			manifest:    `{"worker":}`,
			wantModel:   "",
			wantRuntime: "",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			files := map[string]string{}
			if tc.manifest != "" {
				files["manifest.json"] = tc.manifest
			}
			data := buildZip(t, files)
			gotModel, gotRuntime, gotAdapter, gotParams := extractWorkerFieldsFromZip(data)
			if gotModel != tc.wantModel {
				t.Errorf("model: got %q, want %q", gotModel, tc.wantModel)
			}
			if gotRuntime != tc.wantRuntime {
				t.Errorf("runtime: got %q, want %q", gotRuntime, tc.wantRuntime)
			}
			if gotAdapter != tc.wantAdapter {
				t.Errorf("adapterMode: got %q, want %q", gotAdapter, tc.wantAdapter)
			}
			if fmt.Sprint(gotParams) != fmt.Sprint(tc.wantParams) {
				t.Errorf("runtimeParameter: got %v, want %v", gotParams, tc.wantParams)
			}
		})
	}
}

func TestExtractWorkerFieldsFromZip_NotAZip(t *testing.T) {
	gotModel, gotRuntime, gotAdapter, gotParams := extractWorkerFieldsFromZip([]byte("not a zip"))
	if gotModel != "" || gotRuntime != "" || gotAdapter != "" || gotParams != nil {
		t.Errorf("expected empty fields for non-zip input, got model=%q runtime=%q adapterMode=%q params=%v", gotModel, gotRuntime, gotAdapter, gotParams)
	}
}

func TestWorkerRuntimeHelpIncludesQwenPaw(t *testing.T) {
	for name, usage := range map[string]string{
		"apply":  applyWorkerSubCmd().Flags().Lookup("runtime").Usage,
		"update": updateWorkerCmd().Flags().Lookup("runtime").Usage,
	} {
		if !strings.Contains(usage, "qwenpaw") {
			t.Errorf("%s worker runtime help %q does not include qwenpaw", name, usage)
		}
	}
}
