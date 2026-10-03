// Contract tests use Docker/Moby's matcher, not a gitignore approximation.
package contextcheck

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/moby/patternmatcher"
	"github.com/moby/patternmatcher/ignorefile"
)

var root = filepath.Join("..", "..", "..")
var contexts = map[string]string{
	"backend":  "backend/Dockerfile.scene.dockerignore",
	"frontend": "frontend/Dockerfile.scene.dockerignore",
	"renderer": "renderer/Dockerfile.dockerignore",
}

func matcher(t *testing.T, component string) *patternmatcher.PatternMatcher {
	t.Helper()
	file, err := os.Open(filepath.Join(root, contexts[component]))
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	patterns, err := ignorefile.ReadAll(file)
	if err != nil {
		t.Fatal(err)
	}
	m, err := patternmatcher.New(patterns)
	if err != nil {
		t.Fatal(err)
	}
	return m
}

func expect(t *testing.T, m *patternmatcher.PatternMatcher, path string, included bool) {
	t.Helper()
	ignored, err := m.MatchesOrParentMatches(path)
	if err != nil {
		t.Fatal(err)
	}
	if ignored == included {
		t.Errorf("%s: included=%t, want %t", path, !ignored, included)
	}
}

func TestPrivateStateIsNeverInAnyContext(t *testing.T) {
	paths := []string{
		".env", ".env.production", ".git/config", "tmp/credential-cache.json", "uploads/image.png",
		"backend/instance/settings.json", "backend/instance/credentials.json", "backend/instance/banana.db",
		"backend/instance/cache.py", "backend/server.log", "backend/.env", "backend/.env.local",
		"backend/services/.env.production", "backend/services/__pycache__/private.pyc",
		"backend/services/unknown-credential-cache.json", "backend/services/cert.pem",
		"renderer/node_modules/playwright/index.js", "renderer/.env", "renderer/test-output.pdf",
		"frontend/node_modules/playwright/index.js", "frontend/.env.production", "frontend/dist/index.html",
		"frontend/src/.env", "frontend/src/instance/cached.json", "frontend/public/private.key",
		"docs/fixtures/private.pptx", "unrelated.txt",
	}
	for component := range contexts {
		t.Run(component, func(t *testing.T) {
			m := matcher(t, component)
			for _, path := range paths {
				expect(t, m, path, false)
			}
		})
	}
}

func TestLockedInputsAndSmokeFileReachImage(t *testing.T) {
	data, err := os.ReadFile(filepath.Join(root, "deployment/scene/runtime.lock.json"))
	if err != nil {
		t.Fatal(err)
	}
	var lock struct {
		Files map[string]struct {
			Components []string `json:"components"`
		} `json:"files"`
	}
	if err := json.Unmarshal(data, &lock); err != nil {
		t.Fatal(err)
	}
	for component := range contexts {
		t.Run(component, func(t *testing.T) {
			m := matcher(t, component)
			expect(t, m, "deployment/scene/runtime.lock.json", true)
			for path, entry := range lock.Files {
				for _, role := range entry.Components {
					if role == component {
						expect(t, m, path, true)
					}
				}
			}
		})
	}
}

func TestApplicationFilesAreIncluded(t *testing.T) {
	for component := range contexts {
		t.Run(component, func(t *testing.T) {
			m := matcher(t, component)
			err := filepath.WalkDir(filepath.Join(root, component), func(path string, entry os.DirEntry, err error) error {
				if err != nil {
					return err
				}
				if entry.IsDir() {
					switch entry.Name() {
					case "node_modules", "instance", "__pycache__", "tests", "dist", ".pytest_cache", ".vite":
						return filepath.SkipDir
					}
					return nil
				}
				relative, err := filepath.Rel(root, path)
				if err != nil {
					return err
				}
				relative = filepath.ToSlash(relative)
				required := component == "backend" && strings.HasSuffix(path, ".py") ||
					component == "frontend" && (strings.HasPrefix(relative, "frontend/src/") || strings.HasPrefix(relative, "frontend/public/")) ||
					component == "renderer" && (strings.HasSuffix(path, ".py") || strings.HasSuffix(path, ".mjs") || entry.Name() == "smoke.pptx")
				if required {
					expect(t, m, relative, true)
				}
				return nil
			})
			if err != nil {
				t.Fatal(err)
			}
		})
	}
}
