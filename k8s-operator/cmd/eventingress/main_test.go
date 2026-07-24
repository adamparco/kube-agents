// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/gke-labs/kube-agents/k8s-operator/internal/eventingress"
)

func TestValidate(t *testing.T) {
	tests := []struct {
		name    string
		args    []string
		wantErr bool
	}{
		{"synthetic needs event-file", []string{"--source=synthetic"}, true},
		{"synthetic ok", []string{"--source=synthetic", "--event-file=/tmp/e.json"}, false},
		{"pubsub needs project", []string{"--source=pubsub", "--alert-subscription=s"}, true},
		{"pubsub needs a subscription", []string{"--source=pubsub", "--project=p"}, true},
		{"pubsub ok", []string{"--source=pubsub", "--project=p", "--alert-subscription=s"}, false},
		{"unknown source", []string{"--source=carrier-pigeon"}, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			f, err := parseFlags(tt.args)
			if err != nil {
				t.Fatalf("parseFlags: %v", err)
			}
			if gotErr := f.validate() != nil; gotErr != tt.wantErr {
				t.Fatalf("validate() err=%v, wantErr=%v", f.validate(), tt.wantErr)
			}
		})
	}
}

// TestRunSynthetic proves the Kind terminus end-to-end: a normalized {kind:alert} file is delivered to a
// (fake) seam through the real relay, hitting POST /sessions then /sessions/{id}/inject.
func TestRunSynthetic(t *testing.T) {
	var injected map[string]any
	mux := http.NewServeMux()
	mux.HandleFunc("/sessions", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]string{"sessionID": "k8s-evt-syn01"})
	})
	mux.HandleFunc("/sessions/k8s-evt-syn01/inject", func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		var env struct {
			Message string `json:"message"`
		}
		_ = json.Unmarshal(body, &env)
		_ = json.Unmarshal([]byte(env.Message), &injected)
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "injected"})
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	dir := t.TempDir()
	path := filepath.Join(dir, "alert.json")
	if err := os.WriteFile(path, []byte(`{"kind":"alert","summary":"synthetic burn","policy":"p1"}`), 0o600); err != nil {
		t.Fatalf("write event file: %v", err)
	}

	relay, err := eventingress.NewRelay(eventingress.RelayConfig{
		DaemonURL:      srv.URL,
		BearerToken:    "tok",
		AssertedCaller: "platform",
		HTTPClient:     srv.Client(),
	})
	if err != nil {
		t.Fatalf("NewRelay: %v", err)
	}
	if err := runSynthetic(context.Background(), relay, path); err != nil {
		t.Fatalf("runSynthetic: %v", err)
	}
	if injected["kind"] != "alert" || injected["summary"] != "synthetic burn" {
		t.Fatalf("seam received wrong payload: %#v", injected)
	}
}
