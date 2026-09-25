package svc

import "testing"

func TestVerdictFit(t *testing.T) {
	if got := Verdict(3, 1); got != "fit" {
		t.Fatalf("expected fit, got %s", got)
	}
}

func TestVerdictUnfit(t *testing.T) {
	if got := Verdict(1, 3); got != "unfit" {
		t.Fatalf("expected unfit, got %s", got)
	}
}

func TestVerdictTie(t *testing.T) {
	if got := Verdict(2, 2); got != "unknown" {
		t.Fatalf("expected unknown, got %s", got)
	}
}
