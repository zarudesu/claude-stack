// Package svc implements a small majority-vote check.
package svc

// Verdict returns "yes" when yes votes outnumber no votes, "no" when
// the reverse holds, and "unknown" on a tie.
func Verdict(yes, no int) string {
	if yes > no {
		return "yes"
	}
	if no > yes {
		return "no"
	}
	return "unknown"
}
