package oss

import "fmt"

// buildWorkerPolicy returns the IAM policy for a worker: read/write/list/
// delete on its own agents/<workerName>/ prefix and on shared/, plus (when
// teamName is set) the teams/<teamName>/ prefix, and (when isManager) the
// manager/ prefix. The s3Policy / s3PolicyStatement types live in
// minio_admin.go (shared by both StorageAdminClient providers:
// MinIOAdminClient via `mc admin`, SDKAdminClient via madmin-go).
func buildWorkerPolicy(workerName, bucket, teamName string, isManager bool) s3Policy {
	listPrefixes := []string{
		fmt.Sprintf("agents/%s", workerName),
		fmt.Sprintf("agents/%s/*", workerName),
		"shared",
		"shared/*",
	}
	rwResources := []string{
		fmt.Sprintf("arn:aws:s3:::%s/agents/%s/*", bucket, workerName),
		fmt.Sprintf("arn:aws:s3:::%s/shared/*", bucket),
	}

	if isManager {
		listPrefixes = append(listPrefixes,
			"manager",
			"manager/*",
		)
		rwResources = append(rwResources,
			fmt.Sprintf("arn:aws:s3:::%s/manager/*", bucket),
		)
	}

	if teamName != "" {
		listPrefixes = append(listPrefixes,
			fmt.Sprintf("teams/%s", teamName),
			fmt.Sprintf("teams/%s/*", teamName),
		)
		rwResources = append(rwResources,
			fmt.Sprintf("arn:aws:s3:::%s/teams/%s/*", bucket, teamName),
		)
	}

	return s3Policy{
		Version: "2012-10-17",
		Statement: []s3PolicyStatement{
			{
				Effect:   "Allow",
				Action:   []string{"s3:ListBucket"},
				Resource: []string{fmt.Sprintf("arn:aws:s3:::%s", bucket)},
				Condition: map[string]interface{}{
					"StringLike": map[string]interface{}{
						"s3:prefix": listPrefixes,
					},
				},
			},
			{
				Effect:   "Allow",
				Action:   []string{"s3:GetObject", "s3:PutObject", "s3:DeleteObject"},
				Resource: rwResources,
			},
		},
	}
}
